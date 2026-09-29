"""Generate a fictional dashboard fixture without network access or credentials.

Allocations below illustrate the UI; they are not the actual strategy signals.
Uses the production cash engine so displayed accounting remains consistent.
Run from any directory: python dashboard/scripts/generate_sample.py
"""
import datetime as dt
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'aws/src'))
from shared.portfolio import advance

BOOKS = {
    'tsmom_126': 'TSMOM 6mo',
    'rsi2_lt10_r200': 'RSI-2 + 200d',
    'ml_logreg': 'ML LogReg',
    'xsmom_126_top3': 'Rotation top-3',
    'dualmom_126_top3': 'Dual momentum',
    'ens_2of3': 'Ensemble 2-of-3',
    'buy_and_hold': 'Buy & hold',
}


def generate():
    symbols = ['DEMO_A', 'DEMO_B', 'DEMO_C']
    dates = []
    day = dt.date(2026, 9, 1)
    while len(dates) < 20:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    reports = []
    for book_index, (name, display) in enumerate(BOOKS.items()):
        previous = None
        series = []
        for i, date in enumerate(dates):
            prices = {symbol: 100 + j * 35 + i * (0.24 - j * 0.12)
                      + 2 * math.sin(i / 3 + j) for j, symbol in enumerate(symbols)}
            weights = {symbol: (1 / 3 if name == 'buy_and_hold'
                               or (i // 5 + j + book_index) % 4 != 0 else 0)
                       for j, symbol in enumerate(symbols)}
            previous = advance(previous, prices, weights, hold=name == 'buy_and_hold')
            series.append(dict(date=date, cum_ret=previous['cum_ret'], cum_pnl=previous['cum_pnl']))
        reports.append(dict(previous, name=name, display=display, series=series))
    return dict(
        schema_version=2, sample=True,
        sample_note='Fictional prices, allocations, and account balance. Not actual strategy performance.',
        experiment_id='illustrative-sample', start_date=dates[0], as_of=dates[-1],
        generated_at=dates[-1] + 'T21:15:00+00:00', accounting='shadow_cash_inclusive',
        cost_bps=5, book_budget=12000, model_hash='sample-only',
        account={'equity': 100000}, orders_refreshed=True,
        strategies=reports, targets=[], trades=[],
    )


if __name__ == '__main__':
    target = ROOT / 'dashboard/public/sample-data.json'
    target.write_text(json.dumps(generate(), indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Generated seven fictional books in dashboard/public/sample-data.json')
