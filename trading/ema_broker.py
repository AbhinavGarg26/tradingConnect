"""Explicit Kite adapter and durable quote-based PAPER adapter for EMA strategies."""
from datetime import datetime
from decimal import Decimal,ROUND_DOWN
import time
from zoneinfo import ZoneInfo
from sqlalchemy import select
from trading.strategy_order_intents import StrategyOrderIntent
from trading.strategy_records import StrategyTrade
from strategies.nifty_daily_ema_monthly import quote_fill
from trading.ema_execution import ReconciliationRequired

IST=ZoneInfo('Asia/Kolkata')


class Quotes:
    def __init__(self,kite,store):self.kite,self.store=kite,store

    def quote(self,symbol):
        q=dict(self.kite.quote([symbol])[symbol])
        stamp=q.get('timestamp')
        if isinstance(stamp,datetime) and stamp.tzinfo is None:q['timestamp']=stamp.replace(tzinfo=IST)
        return q

    def spot(self,now):
        q=self.quote('NSE:NIFTY 50');stamp=q.get('timestamp')
        if stamp is None or not 0<=(now-stamp).total_seconds()<=60:raise ValueError('Stale or missing spot quote')
        value=Decimal(str(q.get('last_price') or 0))
        if not value.is_finite() or value<=0:raise ValueError('Invalid spot price')
        return value

    def premium(self,c,quantity,buying,now,settings):
        return quote_fill(self.quote('NFO:'+c['tradingsymbol']),buying=buying,quantity=quantity,now=now,settings=settings)


class KiteBroker(Quotes):
    """Only the CLI's explicit user-selected live mode constructs this adapter."""
    def _positions(self,c):
        return [p for p in self.kite.positions().get('net',[]) if p.get('exchange')=='NFO'
                and p.get('tradingsymbol')==c['tradingsymbol'] and int(p.get('quantity') or 0)!=0]

    def _pending(self,c):
        return [o for o in self.kite.orders() if o.get('exchange')=='NFO' and o.get('tradingsymbol')==c['tradingsymbol']
                and o.get('status') not in {'COMPLETE','REJECTED','CANCELLED'}]

    def check_entry(self,c,quantity,now,settings):
        price=self.premium(c,quantity,True,now,settings)
        if self._positions(c) or self._pending(c):raise ReconciliationRequired('Selected contract is already occupied or has pending orders')
        margin=self.kite.margins('equity')
        available=margin.get('available',{})
        cash=min(Decimal(str(margin.get('net') or 0)),Decimal(str(available.get('cash') or 0)),
                 Decimal(str(available.get('live_balance') or 0)))
        if cash<price*quantity+Decimal('200'):raise ValueError('Insufficient available cash including charge buffer; no paper fallback')

    def check_exit(self,c,quantity,now,settings):
        positions=self._positions(c)
        if (len(positions)!=1 or positions[0].get('product')!='NRML'
            or int(positions[0].get('quantity') or 0)!=quantity or self._pending(c)):
            raise ReconciliationRequired('Owned NRML lot differs from broker or another order is pending')
        self.premium(c,quantity,False,now,settings)

    def verify_owned(self,position):
        if not position:return
        positions=self._positions({'tradingsymbol':position['symbol']})
        if len(positions)!=1 or positions[0].get('product')!='NRML' or int(positions[0]['quantity'])!=position['quantity']:
            raise ReconciliationRequired('Broker-owned lot disappeared or changed; do not submit replacement orders')

    def submit(self,c,side,quantity,tag,now):
        from market.kite_orders import place_protected_market_order
        if side=='BUY':
            tick=Decimal(c.get('tick_size','0.05'))
            if not tick.is_finite() or tick<=0:raise ValueError('Invalid option tick size')
            limit=(Decimal('30000')/quantity/tick).to_integral_value(rounding=ROUND_DOWN)*tick
            # A buy LIMIT, unlike a market order, bounds total paid premium.
            return str(self.kite.place_order(variety='regular',exchange='NFO',tradingsymbol=c['tradingsymbol'],
                transaction_type='BUY',quantity=quantity,product='NRML',order_type='LIMIT',
                price=float(limit),validity='DAY',tag=tag))
        return str(place_protected_market_order(self.kite,variety='regular',exchange='NFO',
            tradingsymbol=c['tradingsymbol'],transaction_type=side,quantity=quantity,product='NRML',
            order_type='MARKET',validity='DAY',tag=tag))

    def orders(self):return self.kite.orders()

    def order(self,order_id):
        last=None
        for _ in range(20):
            matches=[o for o in self.kite.orders() if str(o.get('order_id'))==str(order_id)]
            if matches:last=matches[-1]
            if last and last.get('status') in {'COMPLETE','REJECTED','CANCELLED'}:return last
            time.sleep(.5)
        return last


class PaperBroker(Quotes):
    """Reads quotes, but NEVER calls a broker order endpoint. Receipts survive restarts."""
    def check_entry(self,c,quantity,now,settings):
        self.premium(c,quantity,True,now,settings)
        with self.store.factory() as db:
            occupied=db.scalar(select(StrategyTrade.id).where(StrategyTrade.user_id==self.store.user_id,
                StrategyTrade.mode=='paper',StrategyTrade.option_symbol==c['tradingsymbol'],
                StrategyTrade.status.notin_(['closed','rejected'])).limit(1))
            if occupied:raise ReconciliationRequired('Paper contract is already owned by a strategy')

    def check_exit(self,c,quantity,now,settings):
        position=self.store.position()
        if not position or position['symbol']!=c['tradingsymbol'] or position['quantity']!=quantity:
            raise ReconciliationRequired('Paper exit does not match its owned lot')
        self.premium(c,quantity,False,now,settings)

    def verify_owned(self,position):pass

    def submit(self,c,side,quantity,tag,now):
        from strategies.nifty_daily_ema_monthly import Settings
        price=self.premium(c,quantity,side=='BUY',now,Settings(Decimal('30000')))
        with self.store.factory() as db:
            row=db.scalar(select(StrategyOrderIntent).where(StrategyOrderIntent.user_id==self.store.user_id,
                StrategyOrderIntent.mode=='paper',StrategyOrderIntent.client_tag==tag).with_for_update())
            if row is None or row.status!='submitting':raise ReconciliationRequired('Paper receipt requires a claimed intent')
            if not row.broker_order_id:
                row.broker_order_id='paper-'+tag
                row.filled_quantity=quantity;row.average_price=price;row.updated_at=now
            return row.broker_order_id

    def orders(self):
        with self.store.factory() as db:
            rows=db.scalars(select(StrategyOrderIntent).where(StrategyOrderIntent.user_id==self.store.user_id,
                StrategyOrderIntent.strategy_key==self.store.key,StrategyOrderIntent.mode=='paper',
                StrategyOrderIntent.broker_order_id.is_not(None)))
            return [dict(order_id=r.broker_order_id,tag=r.client_tag,tradingsymbol=r.contract['tradingsymbol'],
                exchange='NFO',product='NRML',quantity=r.quantity,filled_quantity=r.filled_quantity,
                transaction_type='BUY' if r.action in {'entry','roll_entry'} else 'SELL',
                average_price=r.average_price,status='COMPLETE' if r.filled_quantity==r.quantity else 'OPEN') for r in rows]

    def order(self,order_id):
        return next((o for o in self.orders() if o['order_id']==order_id),None)
