"""Replay the same session controller used by the user-operated runner."""
import argparse
import csv
import json
from pathlib import Path

from backtest.fixed_profit import load_sessions, statistics
from backtest.fixed_profit_diagnostics import exit_trade
from strategies.fixed_profit_filtered import FilteredSession, CONFIG, CONFIG_HASH


def replay(bars):
    session=FilteredSession()
    for i,bar in enumerate(bars):
        if session.engine.pending:
            if session.qualify_entry(bar.open,observed_at=bar.time):
                signal=session.engine.pending
                session.engine.confirm_entry(index_price=bar.open)
                direction=1 if signal.action.endswith('CE') else -1
                pnl,end,reason,ambiguous=exit_trade(bars,i,direction,15,30)
                return dict(date=str(bar.time.date()),side=signal.action[-2:],entry_time=bar.time.isoformat(),
                            entry=float(bar.open),stop=float(session.engine.stop),target=float(session.engine.target),
                            points=pnl,exit_bar_time=bars[end].time.isoformat(),exit_reason=reason)
            if session.skipped:return None
        from datetime import timedelta
        session.on_minute(bar,observed_at=bar.time+timedelta(minutes=1))
    return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_path');parser.add_argument('--output',default='outputs/fixed-profit-filtered-runner')
    args=parser.parse_args()
    sessions,_=load_sessions(args.csv_path)
    rows=[r for _,bars in sessions if (r:=replay(bars))]
    prior=Path('outputs/fixed-profit-improvement/low_volatility_clean_entries.csv')
    if prior.exists():
        with prior.open() as f:expected=list(csv.DictReader(f))
        identity=lambda values:[(r['date'],r['side'],r['entry_time'],float(r['entry']),float(r['points'])) for r in values]
        if identity(rows)!=identity(expected):raise ValueError('Runner does not reproduce retained candidate entries/outcomes')
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    with (output/'trades.csv').open('w') as f:
        if rows:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    report=dict(config=dict(CONFIG),config_hash=CONFIG_HASH,matched_saved_candidate=prior.exists(),
                gross=statistics(rows),cost_2=statistics(rows,2))
    (output/'analysis.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
