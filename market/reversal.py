"""Durable, single-owner CE/PE reversals for bought Zerodha and Groww options."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import json
import math
from zoneinfo import ZoneInfo

from sqlalchemy import text
from market.position_stops import hard_loss_reason, position_pnl_pct
from market.entry_price_tracker import CurrentEntryPriceTracker
from market.market_exit import MarketExitExecutor

IST = ZoneInfo('Asia/Kolkata')
DEFAULTS = {
    'reversal_enabled': ('false', 'boolean'),
    'reversal_drawdown_pct': ('5', 'float'),
    'reversal_max_peak_profit_pct': ('10', 'float'),
    'reversal_max_per_day': ('1', 'integer'),
    'reversal_cooldown_seconds': ('300', 'integer'),
    'reversal_confirmation_window_seconds': ('180', 'integer'),
    'reversal_funds_buffer_pct': ('5', 'float'),
    'reversal_daily_loss_limit': ('2000', 'float'),
    'reversal_entry_cutoff': ('15:00', 'string'),
}


def confirmed(side, candles, triggered_at, now):
    """Only a fresh, fully post-trigger minute may confirm a breakout."""
    closed = sorted((c for c in candles if c['date'] + timedelta(minutes=1) <= now),
                    key=lambda c: c['date'])
    if len(closed) < 2:
        return False
    previous, current = closed[-2:]
    if (current['date'] < triggered_at or now - current['date'] >= timedelta(minutes=2)
            or current['date'] - previous['date'] != timedelta(minutes=1)):
        return False
    return (current['close'] < previous['low'] if side == 'CE'
            else current['close'] > previous['high'])


def affordable_quantity(quantity, lot, price, funds, buffer_pct, risk_budget, stop_pct):
    values = (price, funds, buffer_pct, risk_budget, stop_pct)
    if not all(math.isfinite(float(v)) for v in values) or lot <= 0 or price <= 0 or stop_pct <= 0:
        return 0
    cash_lots = math.floor(max(0, funds) / (price * lot * (1 + buffer_pct / 100)))
    risk_lots = math.floor(max(0, risk_budget) / (price * lot * stop_pct / 100))
    return max(0, min(quantity // lot, cash_lots, risk_lots)) * lot


class ReversalWorker:
    def __init__(self, kite, user_id, logger, exit_executor=None, broker_id="zerodha"):
        self.broker_id = broker_id
        self.kite, self.user_id, self.logger = kite, user_id, logger
        self.entries = CurrentEntryPriceTracker()
        self.exits = exit_executor or MarketExitExecutor(kite, logger)
        self.instruments = None
        self.instrument_day = None

    def run(self, stop_event):
        from trading.database import engine, get_db
        from trading.repositories import MarketConfigRepo, RiskOverrideRepo
        from market.auto_exit_config import AutoExitSettings
        settings = AutoExitSettings()
        groww_worker = None
        while not stop_event.is_set():
            try:
                with get_db() as db:
                    settings.refresh(db, self.user_id)
                    cfg = MarketConfigRepo.get_all(db, self.user_id)
                    overrides = RiskOverrideRepo.active_stop_loss_symbols(db, self.user_id)
                    groww_token = None
                    if cfg.get('reversal_enabled', False) and settings.groww_enabled:
                        from trading.exchange_link import ExchangeLinkRepo
                        link = ExchangeLinkRepo.get_for_user(db, self.user_id, provider='groww')
                        if link and link.is_session_valid:
                            groww_token = link.decrypt_session_token(db)
                    if groww_token:
                        from market.groww_reversal import GrowwReversalAdapter, GrowwReversalEntries, GrowwReversalExits
                        if groww_worker is None:
                            adapter = GrowwReversalAdapter(self.kite, groww_token, self.logger, self.user_id)
                            groww_worker = ReversalWorker(adapter, self.user_id, self.logger,
                                GrowwReversalExits(adapter), broker_id='groww')
                            groww_worker.entries = GrowwReversalEntries(adapter)
                        groww_worker.kite.client.access_token = groww_token
                enabled = cfg.get('reversal_enabled', False)
                if enabled and (settings.kite_enabled or (settings.groww_enabled and groww_token)):
                    # Session lock survives the commits required before broker side effects.
                    with engine.connect() as conn:
                        locked = conn.execute(text('SELECT pg_try_advisory_lock(72421, :u)'), {'u': self.user_id}).scalar()
                        conn.commit()
                        if locked:
                            try:
                                workers = ([self] if settings.kite_enabled else [])
                                if settings.groww_enabled and groww_token:
                                    workers.append(groww_worker)
                                # Rotate first evaluation to avoid always favoring one broker.
                                if datetime.now(IST).second % 4 >= 2:
                                    workers.reverse()
                                for worker in workers:
                                    try:
                                        worker.cycle(conn, cfg, settings, overrides)
                                    except Exception:
                                        conn.rollback()
                                        self.logger.exception('%s reversal cycle failed closed', worker.broker_id)
                            finally:
                                conn.rollback()
                                conn.execute(text('SELECT pg_advisory_unlock(72421, :u)'), {'u': self.user_id})
                                conn.commit()
            except Exception:
                self.logger.exception('Reversal monitor failed; no automatic BUY retry')
            stop_event.wait(2)

    def cycle(self, conn, cfg, settings, overrides):
        now = datetime.now(IST)
        if now.weekday() >= 5 or not '09:15' <= now.strftime('%H:%M') < '15:30':
            return
        config = {k: cfg.get(k, float(v) if t in ('float', 'integer') else v)
                  for k, (v, t) in DEFAULTS.items()}
        config['risk_per_trade'] = cfg.get('risk_per_trade', 2000)
        config['max_open_trades'] = cfg.get('max_open_trades', 5)
        for key in ('reversal_drawdown_pct', 'reversal_max_peak_profit_pct',
                    'reversal_confirmation_window_seconds', 'reversal_daily_loss_limit'):
            if not math.isfinite(float(config[key])) or float(config[key]) <= 0:
                return
        if not 0 < float(config['reversal_drawdown_pct']) < 100:
            return
        if not 0 <= float(config['reversal_funds_buffer_pct']) <= 100:
            return
        if not 0 <= float(config['reversal_cooldown_seconds']) <= 86400:
            return
        limit = int(config['reversal_max_per_day'])
        if not 0 <= limit <= 2:
            return
        cutoff = datetime.strptime(str(config['reversal_entry_cutoff']), '%H:%M').time()
        params = {'u': self.user_id, 'd': now.date()}
        conn.execute(text('INSERT INTO market_reversal_states(user_id, trading_date, state) VALUES (:u,:d,\'{}\') ON CONFLICT DO NOTHING'), params)
        state = conn.execute(text('SELECT state FROM market_reversal_states WHERE user_id=:u AND trading_date=:d'), params).scalar()
        if isinstance(state, str):
            state = json.loads(state)
        def save():
            conn.execute(text('UPDATE market_reversal_states SET state=CAST(:s AS jsonb), updated_at=now() WHERE user_id=:u AND trading_date=:d'), {**params, 's': json.dumps(state)})
            conn.commit()
        pending_owner = state.get('pending', {}).get('broker', 'zerodha')
        if state.get('pending') and pending_owner != self.broker_id:
            return
        positions = self.kite.positions().get('net', [])
        pnl = sum(float(p.get('pnl') or 0) for p in positions)
        budget = float(config['reversal_daily_loss_limit']) + min(pnl, 0)
        pending = state.get('pending')
        if pending and pending['status'] in ('DONE', 'SKIPPED'):
            state.setdefault('history', []).append(state.pop('pending'))
            save()
            pending = None
        if pending:
            self.advance(pending, positions, now, cutoff, budget, config, settings, overrides, save)
            return
        if budget <= 0 or now.time() >= cutoff or state.get('count', 0) >= limit:
            return
        if state.get('last_trigger') and (now - datetime.fromisoformat(state['last_trigger'])).total_seconds() < float(config['reversal_cooldown_seconds']):
            return
        if self.instruments is None or self.instrument_day != now.date():
            self.instruments = self.kite.instruments()
            self.instrument_day = now.date()
        by_token = {int(i['instrument_token']): i for i in self.instruments}
        active = [p for p in positions if int(p.get('quantity', 0)) > 0 and p['tradingsymbol'] not in settings.ignored_symbols | overrides]
        self.exits.remove_missing({self.entries.position_key(p) for p in positions if p.get('quantity')})
        for p in active:
            instrument = by_token.get(int(p['instrument_token']), {})
            if instrument.get('instrument_type') not in ('CE', 'PE') or instrument.get('exchange') != 'NFO':
                continue
            symbol = p['tradingsymbol']
            key = self.entries.position_key(p)
            if self.broker_id != "zerodha":
                key = self.broker_id + ":" + key
            entry, _ = self.entries.resolve(self.kite, p)
            quote = self.kite.quote([f"{p['exchange']}:{symbol}"])[f"{p['exchange']}:{symbol}"]
            timestamp = quote.get('last_trade_time')
            if not timestamp or not 0 <= (now - timestamp.astimezone(IST)).total_seconds() <= 15:
                continue
            ltp = float(quote['last_price'])
            # Buy aggregates identify a new lifecycle without resetting on partial SELLs.
            marker = [p.get('buy_quantity'), p.get('buy_value'), entry]
            peaks = state.setdefault('peaks', {})
            old = peaks.get(key)
            if old is None or old['marker'] != marker:
                old = {'marker': marker, 'peak': entry, 'blocked': False}
                peaks[key] = old
            old['peak'] = max(old['peak'], ltp)
            old['blocked'] |= (old['peak'] / entry - 1) * 100 > float(config['reversal_max_peak_profit_pct'])
            save()
            loss = hard_loss_reason(position_pnl_pct(entry, ltp, p['quantity']), settings.hard_stop_loss_pct,
                                    (ltp-entry)*p['quantity'], settings.max_loss_amount)
            if loss or old['blocked'] or ltp > old['peak'] * (1-float(config['reversal_drawdown_pct'])/100):
                continue
            opposite = [i for i in self.instruments if i.get('name') == instrument.get('name')
                        and i.get('exchange') == instrument['exchange'] and i.get('expiry') == instrument['expiry']
                        and i.get('strike') == instrument['strike'] and i.get('instrument_type') == ('PE' if instrument['instrument_type']=='CE' else 'CE')]
            names = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE', 'MIDCPNIFTY': 'NIFTY MID SELECT', 'NIFTYNXT50': 'NIFTY NEXT 50'}
            underlying = [i for i in self.instruments if i.get('exchange') == 'NSE' and i.get('tradingsymbol') == names.get(instrument['name'], instrument['name'])]
            if len(opposite) != 1 or len(underlying) != 1:
                continue
            state['count'] = state.get('count', 0) + 1
            state['last_trigger'] = now.isoformat()
            state['pending'] = {'broker': self.broker_id, 'status': 'EXIT', 'position': p, 'entry': entry,
                'trigger': now.isoformat(), 'side': instrument['instrument_type'],
                'underlying': int(underlying[0]['instrument_token']),
                'target': opposite[0]['tradingsymbol'], 'lot': int(opposite[0]['lot_size']),
                'tick': float(opposite[0]['tick_size']),
                'tag': f"RV{'G' if self.broker_id == 'groww' else 'K'}{self.user_id}{now:%d%H%M%S}"[:20]}
            # Broker positions contain datetimes; persist only execution fields.
            state['pending']['position'] = {k: p[k] for k in ('exchange','tradingsymbol','product','quantity','instrument_token')}
            save()
            self.advance(state['pending'], positions, now, cutoff, budget, config, settings, overrides, save)
            break

    def advance(self, pending, positions, now, cutoff, budget, config, settings, overrides, save):
        status = pending['status']
        if status in ('DONE', 'SKIPPED'):
            # Retain audit history while releasing the single pending slot.
            return
        if status in ('BUY_SUBMITTING', 'BUY_SENT'):
            orders = [o for o in self.kite.orders() if o.get('tag') == pending['tag']]
            if orders:
                pending['order_id'] = orders[-1]['order_id']
                pending['status'] = 'DONE' if orders[-1]['status'] in ('COMPLETE','CANCELLED','REJECTED') else 'BUY_SENT'
                save()
            # Unknown submission outcome intentionally stays blocked for reconciliation.
            return
        p = pending['position']
        trigger = datetime.fromisoformat(pending['trigger'])
        remaining = [x for x in positions if self.entries.position_key(x) == self.entries.position_key(p) and x.get('quantity')]
        expired = (now-trigger).total_seconds() > float(config['reversal_confirmation_window_seconds'])
        veto = expired or budget <= 0 or now.time() >= cutoff or p['tradingsymbol'] in settings.ignored_symbols | overrides
        if remaining:
            current = remaining[0]
            if status != 'EXIT' or int(current['quantity']) <= 0 or int(current['quantity']) > int(p['quantity']):
                pending['status'] = 'SKIPPED'; save(); return
            price = self.kite.ltp([f"{p['exchange']}:{p['tradingsymbol']}"])[f"{p['exchange']}:{p['tradingsymbol']}"]['last_price']
            if hard_loss_reason(position_pnl_pct(pending['entry'], price, current['quantity']), settings.hard_stop_loss_pct,
                                (price-pending['entry'])*current['quantity'], settings.max_loss_amount):
                veto = True
            if veto:
                pending['status'] = 'SKIPPED'; save(); return
            if self.exits.remove_legacy_gtts(current):
                order_id = self.exits.exit_position(current, 'REVERSAL_DRAWDOWN', reference_price=price)
                if order_id:
                    pending['exit_order_id'] = order_id
                save()
            return
        if veto:
            pending['status'] = 'SKIPPED'; save(); return
        orders = self.kite.orders()
        exit_order = next((o for o in orders if o.get('order_id') == pending.get('exit_order_id')), None)
        if not exit_order or exit_order['status'] != 'COMPLETE' or int(exit_order.get('filled_quantity',0)) != int(p['quantity']):
            pending['status'] = 'SKIPPED'; save(); return
        pending['status'] = 'CONFIRM'
        candles = self.kite.historical_data(pending['underlying'], now-timedelta(minutes=6), now, 'minute')
        if not confirmed(pending['side'], candles, trigger, now):
            save(); return
        target = pending['target']
        if target in settings.ignored_symbols | overrides or any(x.get('quantity') and x['tradingsymbol'] == target for x in positions):
            pending['status'] = 'SKIPPED'; save(); return
        if any(o.get('tradingsymbol') in (target,p['tradingsymbol']) and o['status'] not in ('COMPLETE','REJECTED','CANCELLED') for o in orders):
            return
        q = self.kite.quote([f"NFO:{target}"])[f"NFO:{target}"]
        stamp = q.get('last_trade_time')
        asks = q.get('depth', {}).get('sell', [])
        bids = q.get('depth', {}).get('buy', [])
        if not stamp or not 0 <= (now-stamp.astimezone(IST)).total_seconds() <= 15 or not asks or not bids:
            return
        ask, bid = float(asks[0]['price']), float(bids[0]['price'])
        if bid <= 0 or ask < bid or (ask-bid)/ask > .02:
            return
        price = float((Decimal(str(ask)) / Decimal(str(pending['tick']))).to_integral_value(rounding=ROUND_CEILING) * Decimal(str(pending['tick'])))
        margins = self.kite.margins('equity')
        available = margins.get('available', {})
        funds = min(float(margins.get('net', 0)), float(available.get('live_balance', available.get('cash', 0))))
        budget = min(budget, float(config.get('risk_per_trade', 2000)))
        if settings.max_loss_amount is not None:
            budget = min(budget, settings.max_loss_amount)
        quantity = affordable_quantity(int(p['quantity']), pending['lot'], price, funds,
                                      float(config['reversal_funds_buffer_pct']), budget, settings.hard_stop_loss_pct)
        if not quantity:
            pending['status'] = 'SKIPPED'; save(); return
        fresh_positions = self.kite.positions().get('net', [])
        if (any(x.get('quantity') and x['tradingsymbol'] in (target, p['tradingsymbol']) for x in fresh_positions)
                or sum(bool(x.get('quantity')) for x in fresh_positions) >= int(config.get('max_open_trades', 5))
                or sum(float(x.get('pnl') or 0) for x in fresh_positions) <= -float(config['reversal_daily_loss_limit'])
                or datetime.now(IST).time() >= cutoff
                or (datetime.now(IST) - now).total_seconds() > 10):
            pending['status'] = 'SKIPPED'; save(); return
        params = dict(exchange='NFO', tradingsymbol=target, transaction_type='BUY', quantity=quantity,
                      product=p['product'], order_type='LIMIT', price=price, variety='regular', validity='IOC', tag=pending['tag'])
        while quantity:
            params['quantity'] = quantity
            required = self.kite.order_margins([{k:v for k,v in params.items() if k not in ('tag','validity')}])[0]
            if float(required['total']) * (1+float(config['reversal_funds_buffer_pct'])/100) <= funds:
                break
            quantity -= pending['lot']
        if not quantity or datetime.now(IST).time() >= cutoff or not 0 <= (datetime.now(IST)-stamp.astimezone(IST)).total_seconds() <= 15:
            pending['status'] = 'SKIPPED'; save(); return
        pending.update(status='BUY_SUBMITTING', quantity=quantity, price=price)
        save()  # durable BEFORE network call: timeout cannot cause a second BUY
        pending['order_id'] = self.kite.place_order(**params)
        pending['status'] = 'BUY_SENT'
        save()
