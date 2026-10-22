"""PostgreSQL journal for frozen V3, isolated from the older 15m strategy key."""
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, func, text
from trading.database import get_db
from trading.strategy_records import StrategyRun, StrategyTrade, StrategyEvent

KEY = 'nifty_30m_reference'
VERSION = 'v3'


def verify_schema(db):
    nullable = db.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='strategy_trades' AND column_name='target'")).scalar()
    if nullable != 'YES':
        raise RuntimeError('Apply allow_strategy_trades_without_target.sql before running frozen V3')


class ReferenceRecorder:
    def __init__(self, user_id, today, *, live):
        self.user_id, self.today, self.live = user_id, today, live
        self.trade_id = None
        now = datetime.now(timezone.utc)
        with get_db() as db:
            verify_schema(db)
            prior = db.scalar(select(func.count()).select_from(StrategyTrade).where(
                StrategyTrade.user_id == user_id, StrategyTrade.strategy_key == KEY,
                ((StrategyTrade.trading_date == today) |
                 StrategyTrade.status.in_(['open', 'entry_pending', 'exit_pending', 'reconcile']))))
            if prior:
                raise RuntimeError('Prior or unresolved V3 trades found; do not restart without reconciliation')
            row = StrategyRun(user_id=user_id, strategy_key=KEY, strategy_version=VERSION,
                              mode='live' if live else 'paper', status='running', started_at=now,
                              heartbeat_at=now, configuration={
                                  'stop_points': '25', 'max_entries': 3, 'opening_body_min': '20',
                                  'buffer_percent': '5', 'entry_timeframe_minutes': 1,
                                  'reference_minutes': 30, 'reference_exit_buffer': '0',
                                  'lot_count': 1, 'strike_offset': 2, 'expiry': 'nearest_unexpired',
                                  'conflict_direction': 'lower_strike', 'funds_fallback': 'paper',
                                  'experimental_filters': False})
            db.add(row); db.flush(); self.run_id = row.id

    def heartbeat(self):
        with get_db() as db:
            row = db.get(StrategyRun, self.run_id)
            row.heartbeat_at = row.updated_at = datetime.now(timezone.utc)

    def event(self, action, reason, *, signal=None, details=None):
        with get_db() as db:
            db.add(StrategyEvent(strategy_run_id=self.run_id, strategy_trade_id=self.trade_id,
                                 occurred_at=signal.time if signal else datetime.now(timezone.utc),
                                 action=action, reason=reason,
                                 index_price=signal.index_price if signal else None,
                                 stop=signal.stop if signal else None, target=None,
                                 details=details or {}))

    def begin_entry(self, signal, contract, quantity, number, index_price, *, paper):
        with get_db() as db:
            count = db.scalar(select(func.count()).select_from(StrategyTrade).where(
                StrategyTrade.user_id == self.user_id, StrategyTrade.strategy_key == KEY,
                StrategyTrade.trading_date == self.today,
                StrategyTrade.status != 'rejected'))
            if count >= 3 or number > 3:
                raise RuntimeError('Three V3 entries already reserved today')
            stop = index_price + (25 if signal.action.endswith('PE') else -25)
            row = StrategyTrade(strategy_run_id=self.run_id, user_id=self.user_id,
                                strategy_key=KEY, strategy_version=VERSION,
                                mode='paper' if paper else 'live', trading_date=self.today,
                                trade_number=number, status='entry_pending', side=signal.action[-2:],
                                option_symbol=contract['tradingsymbol'], option_expiry=contract['expiry'],
                                option_strike=contract['strike'], lot_count=1, quantity=quantity,
                                entry_signal_time=signal.time, entry_reason=signal.reason,
                                entry_index=index_price, initial_stop=stop, current_stop=stop,
                                target=None, reference_high=signal.ce_reference,
                                reference_low=signal.pe_reference)
            db.add(row);db.flush();self.trade_id=row.id
        self.event(signal.action, signal.reason, signal=signal,
                   details={'option_symbol': contract['tradingsymbol'], 'quantity': quantity,
                            'selected_strike': str(contract['strike']), 'expiry': str(contract['expiry']),
                            'reference_ce': str(signal.ce_reference), 'reference_pe': str(signal.pe_reference),
                            'mode': 'paper' if paper else 'live'})

    def order_submitted(self, order_id, *, exit_order):
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id)
            if exit_order: row.exit_order_id=order_id
            else: row.entry_order_id=order_id
            row.updated_at=datetime.now(timezone.utc)
        self.event('ORDER_SUBMITTED', 'exit' if exit_order else 'entry', details={'order_id': order_id})

    def entry_filled(self, order_id, premium, index_price, *, paper, fallback_reason):
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id)
            row.mode='paper' if paper else 'live'
            row.entry_order_id=order_id
            row.entry_premium=premium
            row.entry_index=index_price
            row.status='open'
            row.entry_confirmed_at=row.updated_at=datetime.now(timezone.utc)
            if fallback_reason:row.error_message='Paper fallback: '+fallback_reason[:1500]
        self.event('ENTRY_FILLED','insufficient_funds_paper_fallback' if fallback_reason else 'confirmed',
                   details={'mode': 'paper' if paper else 'live', 'premium': str(premium),
                            'fallback_reason': fallback_reason, 'fill_source': 'quote_simulation' if paper else 'broker_order'})

    def set_entry_index(self, price):
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id)
            row.entry_index=price
            row.initial_stop=row.current_stop=price+(25 if row.side=='PE' else -25)
            row.updated_at=datetime.now(timezone.utc)

    def reference_update(self, update):
        self.event('REFERENCE_UPDATE','completed_30m_body',details={k:str(v) for k,v in update.items()})

    def exit_intent(self, signal, price, *, paper):
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id,with_for_update=True)
            if row.status!='open':raise RuntimeError('Trade no longer open; exit ownership changed')
            row.exit_signal_time=signal.time
            row.exit_reason=signal.reason
            row.exit_index=price
            row.status='exit_pending'
            direction=1 if row.side=='CE' else -1
            row.index_points=direction*(price-row.entry_index)
            row.updated_at=datetime.now(timezone.utc)
        self.event(signal.action,signal.reason,signal=signal,details={'mode':'paper' if paper else 'live'})

    def exit_filled(self, order_id, premium):
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id)
            row.exit_order_id=order_id
            row.exit_premium=premium
            row.gross_option_pnl=(premium-row.entry_premium)*row.quantity
            row.exit_confirmed_at=row.updated_at=datetime.now(timezone.utc)
            row.status='closed'
        self.trade_id=None

    def uncertain(self, message):
        if self.trade_id is None:return
        with get_db() as db:
            row=db.get(StrategyTrade,self.trade_id)
            if row.status!='closed':
                row.status='reconcile';row.error_message=message[:2000]
                row.updated_at=datetime.now(timezone.utc)

    def finish(self, status, message=None):
        with get_db() as db:
            row=db.get(StrategyRun,self.run_id)
            row.status=status;row.error_message=message[:2000] if message else None
            row.ended_at=row.heartbeat_at=row.updated_at=datetime.now(timezone.utc)
