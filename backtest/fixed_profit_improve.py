"""Entry-filter research on saved 307-entry / 15-stop baseline diagnostics.

No new minute replay. Every retained trade uses its previously computed 15-point
exit. Rejecting an entry skips that whole day; no later replacement entry.
"""
import argparse
import csv
import json
from pathlib import Path

from backtest.fixed_profit_diagnostics import periods


def load_rows(path):
    with open(path) as handle:
        rows=list(csv.DictReader(handle))
    for row in rows:
        for key in ('stop15_points','atr5','crossings','entry_distance','body_fraction','close_location'):
            row[key]=float(row[key])
        row['early']=row['early']=='True'
        row['atr_ready']=row['atr_ready']=='True'
    return rows


def filter_rules():
    return {
        'baseline':lambda r:True,
        'before_noon':lambda r:r['early'],
        'clean_level':lambda r:r['crossings']<=2,
        'low_volatility':lambda r:r['atr_ready'] and r['atr5']<=20,
        'near_level':lambda r:r['entry_distance']<=5,
        'strong_close':lambda r:r['body_fraction']>=.5 and r['close_location']>=.75,
        'low_volatility_clean':lambda r:r['atr_ready'] and r['atr5']<=20 and r['crossings']<=2,
        'early_clean':lambda r:r['early'] and r['crossings']<=2,
    }


def analyze(path,output):
    rows=load_rows(path)
    rules=filter_rules()
    results={name:periods([r for r in rows if rule(r)],'stop15_points') for name,rule in rules.items()}
    eligible=[name for name in rules if name!='baseline' and results[name]['train']['gross']['trades']>=40]
    selected=max(eligible,key=lambda n:results[n]['train']['cost_2']['avg_points']) if eligible else None
    # Do not pick a different rule after observing this gate failure.
    passed=bool(selected and all(results[selected][p]['cost_2']['net_points']>0 for p in ('train','validation','test'))
                and results[selected]['validation']['gross']['trades']>=20
                and results[selected]['test']['gross']['trades']>=10)
    report=dict(source=str(path),provenance='Saved pre-entry features and previously computed 15-point exits; no new market replay',
                selection='Highest training mean after 2-point deduction, >=40 training trades; no later-period reselection',
                selected_on_training=selected,passed_research_gate=passed,comparisons=results)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/'analysis.json').write_text(json.dumps(report,indent=2))
    for name in ('baseline',selected):
        if name is None:continue
        subset=[r for r in rows if rules[name](r)]
        exported=[]
        for row in subset:
            entry=float(row['entry']);direction=1 if row['side']=='CE' else -1
            exported.append(dict(date=row['date'],side=row['side'],entry_time=row['entry_time'],
                                 entry=entry,stop=entry-direction*15,target=float(row['target']),
                                 points=row['stop15_points'],atr5=row['atr5'],atr_ready=row['atr_ready'],
                                 crossings=row['crossings'],original_10_stop_points=float(row['points'])))
        with (output/f'{name}_entries.csv').open('w') as handle:
            if exported:
                writer=csv.DictWriter(handle,fieldnames=list(exported[0]));writer.writeheader();writer.writerows(exported)
    print('Selected:',selected,'passed research gate:',passed)
    for name,r in results.items():
        print(name,[(p,r[p]['gross']['trades'],r[p]['gross']['win_pct'],r[p]['gross']['net_points'],r[p]['cost_2']['net_points']) for p in ('train','validation','test','all')])
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='outputs/fixed-profit-diagnostics/trade_diagnostics.csv')
    parser.add_argument('--output',default='outputs/fixed-profit-improvement')
    args=parser.parse_args();analyze(args.source,args.output)
