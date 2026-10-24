"""Read-only database readiness audit. Never enables services or submits orders."""
import argparse
import json
from sqlalchemy import inspect,select
from nifty_15m_env import load_nifty_environment


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--user-id',type=int,required=True)
    p.add_argument('--mode',choices=('paper','live'),default='live')
    args=p.parse_args()
    load_nifty_environment()
    from trading.database import get_db
    from trading.strategy_order_intents import StrategyExecutionState,StrategyOrderIntent
    keys=('nifty_daily_ema_monthly','nifty_bearish_15m_9_21')
    with get_db() as db:
        schema=inspect(db.bind)
        missing=[t for t in ('strategy_execution_states','strategy_order_intents') if not schema.has_table(t)]
        if missing:
            print(json.dumps({'ready':False,'missing_tables':missing,'action':'Apply Rails migrations; order execution stays disabled'},indent=2))
            return
        if 'applied_at' not in {c['name'] for c in schema.get_columns('strategy_order_intents')}:
            print(json.dumps({'ready':False,'missing_columns':['strategy_order_intents.applied_at'],
                'action':'Apply Rails migration 20261024030000; no activation performed'},indent=2))
            return
        indexes=schema.get_indexes('strategy_order_intents')
        unique_intent=any(i.get('unique') and i.get('column_names')==['idempotency_key'] for i in indexes)
        report={'read_only':True,'live_activation_performed':False,'intent_unique_index_present':unique_intent,'strategies':{}}
        for key in keys:
            state=db.scalar(select(StrategyExecutionState).where(StrategyExecutionState.user_id==args.user_id,
                StrategyExecutionState.strategy_key==key,StrategyExecutionState.mode==args.mode))
            pending=list(db.scalars(select(StrategyOrderIntent).where(StrategyOrderIntent.user_id==args.user_id,
                StrategyOrderIntent.strategy_key==key,StrategyOrderIntent.mode==args.mode,
                StrategyOrderIntent.status.in_(['submitting','submitted','uncertain']))))
            report['strategies'][key]={'checkpoint_present':state is not None,
                'status':state.status if state else 'not_initialized','position_trade_id':state.position_trade_id if state else None,
                'unresolved_intents':[{'id':r.id,'status':r.status,'broker_order_id':r.broker_order_id,'client_tag':r.client_tag} for r in pending],
                'runner_integration':'implemented; use ema_strategy_runner.py --check for broker/data readiness'}
        from trading.strategy_records import StrategyRun,StrategyTrade
        for key in ('nifty_15m_breakout','nifty_30m_reference','fixed_profit_filtered'):
            unresolved=list(db.scalars(select(StrategyTrade).where(StrategyTrade.user_id==args.user_id,
                StrategyTrade.strategy_key==key,StrategyTrade.mode==args.mode,StrategyTrade.status!='closed')))
            run=db.scalar(select(StrategyRun).where(StrategyRun.user_id==args.user_id,
                StrategyRun.strategy_key==key,StrategyRun.mode==args.mode).order_by(StrategyRun.started_at.desc()).limit(1))
            report['strategies'][key]={'mode':args.mode,'latest_run_status':run.status if run else 'not_initialized',
                'runner_integration':'implemented; database session journal',
                'restart_policy':'fail_closed; reconcile prior session/reservations before restarting',
                'unresolved_trades':[{'id':r.id,'status':r.status,'entry_order_id':r.entry_order_id,
                    'exit_order_id':r.exit_order_id,'symbol':r.option_symbol} for r in unresolved]}
        report['mode']=args.mode
        print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
