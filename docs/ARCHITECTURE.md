# V2 Architecture

## Single-source strategy rule

`app/engine/strategy_reference_v18.py` remains the signal-generation source of truth.

Web pages, backtests and execution modules must not reimplement A/B/C entry rules.

## Layers

1. **DataProvider**
   - AKShare / Tushare
   - normalized daily contract: date/open/high/low/close/volume/amount/turnover

2. **V18 Signal Engine**
   - previous-completed-week trend filter
   - A / B / C daily entry structures
   - close-entry Golden regression

3. **Execution Engine**
   - close or next_open
   - frozen T-day signal + FAIL
   - next-open price changes entry risk / target weight / MFE
   - independent A/B/C exit state machines

4. **Portfolio Engine**
   - daily mark-to-market
   - structural risk sizing
   - K capacity
   - max one C
   - A/B > C; C can yield to new A/B
   - Monte Carlo same-day ordering

5. **Web**
   - dashboard
   - N-day screener
   - structure chart
   - next-open sizing
   - execution backtest
   - portfolio backtest
   - Golden validation

## Correctness gates

- Entry-engine edits require Golden event regression.
- Execution-engine edits require close-mode regression plus next-open tests.
- Portfolio edits require mark-to-market tests.
- UI cannot change strategy semantics.
