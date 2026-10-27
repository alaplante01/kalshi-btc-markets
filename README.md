# kalshi-btc-markets

Python tools for studying Kalshi's bitcoin prediction markets: an asyncio collector that records the exchange's BTC market data and the CF Benchmarks settlement index into SQLite, a terminal that prices the 15-minute contracts with a closed-form model, and an order CLI that sends nothing unless told to. It is a parked personal project, written between 28 September and 5 October 2026 and run until 10 October 2026; nothing here is running now.

This is a personal research tool, not trading advice. See [Disclaimer](#disclaimer).

## Overview

The code tracks three Kalshi series: `KXBTC15M` (15-minute up/down contracts), `KXBTC` (price ranges) and `KXBTCD` (above/below a strike). `KXBTC` and `KXBTCD` each list hourly events, with ranges and strikes $100 apart, plus daily and weekly events that close at 17:00 ET. All three settle on the CF Benchmarks Bitcoin Real Time Index (BRTI), averaged over its 60 one-second values in the final minute.

| File | What it does |
| --- | --- |
| `collector.py` | Streams BRTI and market data over two WebSockets into SQLite |
| `backfill.py` | Pulls 1-minute candles and the public trade tape over REST |
| `terminal.py` | Live fair value and signals for the open 15-minute contract |
| `trade.py` | Orders, cancels, positions and P&L against Kalshi's v2 API |
| `kalshi_client.py` | Request signing and a small REST client |
| `capture_stats.py` | Read-only summary of a collector database |

## Status

- Parked. The code was written 28 Sep – 5 Oct 2026; the collector ran, with gaps, from 29 Sep to 10 Oct 2026.
- It ran against Kalshi's production exchange, and `trade.py` was used for real orders in early October 2026. No strategy was evaluated systematically, and this README reports no trading results.
- Preparing the repo for publication added the tests, `capture_stats.py`, and a configurable credential directory and API hosts. It also turned up the bugs listed under [Limitations](#limitations); the two that affected data capture and P&L accounting were [fixed at publication](#fixed-at-publication), and their tests now pass.
- `.github/workflows/ci.yml` runs the tests with pytest on Python 3.14 for pushes to `main` and for pull requests.

## Architecture

```text
          Kalshi WebSocket API (signed handshake)
 conn A: BRTI 1 s + 5 Hz          conn B: ticker + trade for every tracked market,
 (stable)                         orderbook_delta for up to 40 of them
    |                             (rebuilt when discovery changes the set)
    | put_nowait                          | put_nowait
    v                                     v
 +--------------------------------------------------+
 |          asyncio.Queue(maxsize=100_000)          | <-- market metadata rows from
 +--------------------------------------------------+     REST discovery every 30 s
                          |                               (run in a worker thread)
                          v
 writer(): the only SQLite connection
 WAL, busy_timeout 5 s, commit at 200 rows or 0.5 s
                          |
                          v
                   data/kalshi.db  <--- backfill.py (REST, separate process,
                          |                          a second writer)
                          | read-only
                          v
 terminal.py: fair value + signals --> signals.log

 trade.py --signed REST--> Kalshi order API (dry run unless --send)
```

- **Single writer.** Feed handlers parse each message and `put_nowait` it onto the bounded queue; only `writer()` touches SQLite, so nothing in the feed path waits on the disk.
- **Batched commits.** `writer()` commits when 200 rows are pending or 0.5 s has passed, whichever comes first. WAL mode lets the terminal read while it writes. The backfill is a second writer. It commits after inserting each market row, after its candles and after its trades, so no transaction is open during either network fetch.
- **Reconnects.** Both sockets reconnect with exponential backoff from 1 s to a 60 s cap, reset after each successful connect, with WebSocket pings every 10 s.
- **Order-book sequence gaps.** Book messages carry a per-subscription `seq`; a jump is logged with the subscription id. The body of each snapshot and delta is stored as JSON so books can be rebuilt offline; `seq` itself is not stored.
- **Discovery.** Every 30 s a REST pass lists each series' open events with their markets and records a snapshot of every active market. When the wanted set changes, as it does each time a 15-minute contract rolls over, connection B is cancelled and reopened with the new set.
- **Idempotent inserts.** Trades are keyed on `trade_id` with `INSERT OR IGNORE`, so the live feed and the backfill can overlap.

## Getting started

This needs Python 3.11 or newer. The tests pass on 3.11 and 3.14.

```bash
git clone https://github.com/alaplante01/kalshi-btc-markets.git
cd kalshi-btc-markets
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`collector.py` and `trade.py` sign requests with a Kalshi API key, and every `trade.py` command loads it, dry runs included; `backfill.py` and `terminal.py` use public endpoints only. Keep the private key outside the repo (chmod 600):

```bash
export KALSHI_KEY_ID=...                      # API key id from Kalshi account settings
export KALSHI_PEM=~/.kalshi/kalshi-prod.pem   # RSA or Ed25519 private key (PEM)
# or put key-id.txt and kalshi-prod.pem in $KALSHI_CRED_DIR (default ~/.kalshi)
```

To point the code at Kalshi's demo exchange, which needs its own keys (untested: the run used production only, and it was never checked whether demo lists these series):

```bash
export KALSHI_REST_BASE=https://external-api.demo.kalshi.co
export KALSHI_WS_URL=wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
```

Run each piece:

```bash
.venv/bin/python collector.py             # BRTI + KXBTC15M, KXBTC, KXBTCD -> data/kalshi.db
.venv/bin/python backfill.py --series KXBTC15M --hours 48    # --repair fills only missing markets
.venv/bin/python terminal.py --edge 0.08  # reads data/kalshi.db; needs the collector running
.venv/bin/python capture_stats.py data/kalshi.db
.venv/bin/python trade.py balance         # also: orders, positions, fills, settlements, analyze, quote TICKER
.venv/bin/python trade.py group --limit 400 --send            # exchange-side order group, saved and reused
.venv/bin/python trade.py yes TICKER --count 50 --at 0.55     # dry run: prints the payload
.venv/bin/python trade.py no TICKER --count 50 --at 0.57 --taker --send
.venv/bin/python trade.py cancel ORDER_ID --market TICKER --send
.venv/bin/python trade.py cancel-all --send
```

## Tests

```bash
.venv/bin/python -m unittest -v
```

There are 49 tests, and all run offline: the signing tests generate throwaway keys and the order tests stub the HTTP call. They cover the fee and pricing formulas, the signal rule, message parsing, the writer's timed commit and trade dedup, the market-feed handler including sequence-gap logging, discovery, the backfill's timestamps and 429 backoff, request signing, the REST pager, order construction, the client-side guards and realized P&L. Two pin the bugs that were fixed at publication (below).

## Results: what was measured

A run in Sep–Oct 2026 captured 49.7 million live market-data rows (49,744,106) in about 170 hours of capture spread over an 11.7-day window. The table and the coverage figures are printed by `capture_stats.py` for that run's database; the operations and signal figures come from the run's `collector.log` and `signals.log`, which are not in the repo.

| Table | Rows | Detail |
| --- | --: | --- |
| `trades`, live feed | 25,393,797 | 7,833 markets; 21,921,979 of the trades are in 756 fifteen-minute windows |
| `ticker` | 17,523,011 | quote, volume and open-interest updates for 69,324 markets |
| `brti` | 3,600,438 | 600,087 one-second and 3,000,351 five-per-second index values |
| `book` | 3,226,860 | 3,157,380 deltas and 69,480 snapshots (raw JSON) for 6,537 `KXBTC` range markets |
| `trades`, REST backfill | 3,625,851 | |
| `candles` | 123,683 | 1-minute candles from the backfill |
| `backfill_markets` | 5,010 | 4,910 `KXBTC` range markets and 100 fifteen-minute markets |
| `markets` | 10,746,574 | discovery snapshots of 76,355 markets |

- **Coverage.** The window runs from 2026-09-29 04:56:30Z to 2026-10-10 21:16:16Z. One-second index values exist in 10,174 distinct minutes (169.6 hours). There are 171 gaps longer than a minute, 113.6 hours in total; the longest is 51.2 hours (3–5 October).
- **Method.** Counts come from the 12,147,314,688-byte main database file opened with `immutable=1`. About 40 MB of write-ahead log had not been checkpointed into it and is not counted. Live and backfilled trades are told apart by timestamp precision (the feed stores milliseconds, the backfill whole seconds), so a live trade that lands exactly on a whole second counts as backfill.
- **Operations.** The surviving collector log covers 5–10 October only. In that period discovery tracked at most 637 markets and 40 order books at once, and 369 feed errors were logged (313 closed connections, 45 DNS failures, 11 timeouts). Every error was followed by a backoff and a reconnect attempt, and no order-book sequence gaps were logged.
- **Signals.** `signals.log` holds 3,994 lines over 157 fifteen-minute windows between 30 September and 3 October (UTC), appended by two terminal processes running at the same time. 1,688 lines are in the format of the version in this repo (652 TAKE YES, 1,034 TAKE NO, 2 LOCKED). The other 2,306 (904 TAKE YES, 1,400 TAKE NO, 2 LOCKED) came from an earlier version that is not in the repo and still fired inside the last two minutes, 426 times. Each of the two LOCKED events was logged by both processes. A line is written whenever the signal text changes, so the lines are not independent calls. They have not been scored against settlement, and no accuracy or edge is claimed.

The data itself is not published: Kalshi's Developer Agreement does not allow sharing it.

## Design notes

### Fair-value model (`terminal.py`)

Once a second the terminal reads the database (read-only) and prices the open 15-minute contract.

- **Volatility.** σ is the standard deviation of one-second BRTI changes over the trailing 20 minutes (`VOL_WINDOW_S = 1200`), refreshed every 30 s. The spot input S is the mean of the last five one-second values.
- **Before the settlement minute** BRTI is treated as a driftless Gaussian random walk: P(YES) = Φ((S − K) / (σ·√(t − 30))), where K is the strike and t the seconds to close.
- **Inside the settlement minute**, n of the 60 values are known, with running average A. With r = 60 − n values to come, the projected settlement is (A·n + S·r) / 60 and its standard deviation is σ·√(r³/3) / 60: the sum of the next r points of a random walk has a variance of about σ²·r³/3.
- **Fee.** The taker fee model is 0.07·P·(1 − P) per contract, 1.75¢ at P = 0.50.
- **Signal rule.** TAKE YES when P − ask ≥ fee(ask) + edge. TAKE NO when (1 − P) − (1 − bid) ≥ fee(1 − bid) + edge, where 1 − bid is the cost of NO. The edge buffer defaults to 8¢ (`--edge 0.08`).
- **LOCKED.** Inside the settlement minute, when P is above 0.999 (or below 0.001) and the winning side still costs 0.94 or less.
- **The last two minutes.** On 29 September a spot check found the model wrong on 8 of 11 calls at the one-minute horizon, so signals inside the last two minutes are limited to LOCKED states. The comment in `terminal.py` is the only record of that check; the 11 cases are not in the repo.

### Execution CLI (`trade.py`)

- **Dry run by default.** `group`, `yes`, `no`, `cancel` and `cancel-all` print what they would send and send nothing without `--send`. Read commands run immediately.
- **Signing.** Each request signs timestamp + method + path (query string excluded) with RSA-PSS (SHA-256, MGF1-SHA-256, digest-length salt) or Ed25519, depending on the key, and sends it in `KALSHI-ACCESS-*` headers.
- **Orders.** `yes` sends a bid on the YES book; `no` sends an ask at 1 − price, so buying NO at 0.57 is selling YES at 0.43. Maker orders are post-only and good till cancelled; `--taker` sends immediate-or-cancel. Every order goes to exchange shard 2 (crypto) with `taker_at_cross` self-trade prevention and a fresh UUID `client_order_id`.
- **Order group.** `group --limit N --send` creates an exchange-side order group. If more than N contracts fill within any 15 seconds, the exchange cancels the group's resting orders and rejects new ones until the group is reset. The group id is saved to `$KALSHI_CRED_DIR/order-group.json` and attached to every later order.
- **Client-side guards.** The constants are at the top of `trade.py`:
  - `MAX_CONTRACTS = 200` per order unless `--force`. This is checked on every order, dry runs included.
  - `NET_POSITION_CAP = 300`: the net position in the market after the order must stay within ±300 contracts.
  - `DAILY_STOP = 50.0`: no order that adds risk is allowed once realized P&L on markets settled in the last 24 hours is at or below −$50. Orders that reduce the position skip this check, so it never blocks an exit.

  The position cap and the loss brake need authenticated reads, so they run only with `--send`.
- **`analyze`** walks the fill and settlement history and prints the 12 worst settled markets by P&L (with estimated fees, fill count and side flips), the 5 best, and P&L, fees and contracts per UTC day.

## Limitations

### Fixed at publication

Both were found while preparing the repo for publication, after the October run, and fixed before it; the run's data carries both defects. Each fix is pinned by a test.

1. **No order books for the 15-minute markets.** `discover()` capped the book list at 40 after sorting it alphabetically, and every `KXBTC-…` ticker sorts before `KXBTC15M-…`, so all 3,226,860 book rows from the run belong to `KXBTC` range markets and the only 15-minute quotes are from the ticker channel. The 15-minute tickers now take book slots first. The "quoted" test still uses `or`, so any hourly market with an ask at or below 0.98 qualifies for one of the remaining slots even with no bid.
2. **Fill direction in P&L.** `trade.py` read a fill's direction from `taker_outcome_side`, which only public trades carry, and fell back to the deprecated `side`, which names the opposite outcome when `action` is `sell`. Realized P&L from `pnl_24h()` and `analyze`, and with it the loss brake, was wrong for sell fills, and `fills` printed the wrong direction for them. `fill_side()` now reads Kalshi's `outcome_side` and derives the direction from `side` and `action` only when it is missing. Fees are still estimated from the formula rather than read from the fill's `fee_cost`.

### Other limitations

- **Lossy backpressure.** If the queue fills, `put_nowait` raises and the feed handles it like a dropped connection: the message is lost and the socket reconnects.
- **Gaps are logged, not repaired.** Nothing resubscribes or fetches a fresh snapshot after a sequence gap, and since `seq` is not stored, the database alone cannot show where gaps were.
- **Model assumptions.** The model assumes constant volatility, no drift, and no jumps or fat tails. The pre-window horizon t − 30 overstates the variance of a 60-value average, most of all just before the window opens. Inside the window, the variance σ²·r³/3 is the continuous-time form of the discrete σ²·r(r+1)(2r+1)/6, which is larger, so the model is too confident when few values remain. The five-value smoothing lags the latest print. Apart from the 11-case spot check, the model has not been tested against outcomes.
- **Scope.** The terminal prices only the open 15-minute contract. `KXBTC` and `KXBTCD` are recorded but not priced.
- **No retries in the order path.** Each call generates a new `client_order_id`, so re-running after a timeout places a second order.
- **Storage.** Everything goes into one SQLite file with no partitioning or retention. The run's file reached 12.1 GB.

## Disclaimer

This is a personal research project. It is not investment or trading advice, not an offer of any product, and not affiliated with or endorsed by Kalshi or CF Benchmarks. Event contracts can lose their entire purchase price, and with `--send`, `trade.py` places real orders with real money. Use it at your own risk and within Kalshi's terms.

## License

MIT. See [LICENSE](LICENSE).
