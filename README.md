# OptionsDashboard

A local NIFTY/SENSEX trading dashboard with broker WebSocket data, dry execution, and guarded live TBS automation. The Kotak Neo SDK is bundled under `vendor/kotak_neo`; no sibling repository is required.

## Daily start on Windows

1. Double-click **Start-OptionsDashboard.cmd**. It opens the dashboard at **http://127.0.0.1:8765**. The server runs in the background.
2. Click **Connect broker**, keep **Use saved credentials** selected, and enter your current six-digit TOTP. Login is required each day; TOTP/session tokens are not saved.
3. Check **Dry/Live**, the selected underlying, expiry, and TBS status. A new dry session starts its first paper straddle after the first chain loads. To run the other underlying too, select it and click **Start dry TBS**. Recovered sessions stay paused until resumed.
4. Keep the computer awake through the **15:10 IST** strategy exit. Closing the browser does not stop the server. Use **Stop-OptionsDashboard.cmd** after TBS has exited; it refuses to stop while a strategy is still active.

On this PC, the Python environment and dependencies are already prepared. On a fresh PC, install **uv** or **Python 3.12** first; the launcher creates `.venv` and installs the pinned `requirements.lock` automatically. Internet access is needed for the initial dependency download and for broker data. Repeated clicks reuse the running server.

The server binds to loopback only. Do not run multiple workers or instances against the same `.local` database.

## Moving from the original terminal

The local migration marker in `.local/migration.json` points to the original terminal's data folder. While the original terminal is running, the launcher opens that existing session instead of creating a second scheduler. After the original server is closed, the first launch copies its latest SQLite ledger and encrypted credentials into this project. The migration runs once and refuses to overwrite an existing destination ledger. Keep the original folder until this first launch succeeds.

The migration marker is specific to this PC and is excluded from GitHub. A fresh GitHub checkout starts with no account data. Windows-encrypted credentials work only for the Windows account that saved them.

## GitHub contents

Git tracks the application, broker SDK and its license, launchers, tests, documentation, and exact dependency versions. It excludes `.local`, `.venv`, `env.txt`, `.env` files, encrypted credentials, databases, caches, and logs. Enter credentials through the application; `env.txt` is preserved locally but is not loaded by this app. GitHub Actions runs the tests on Windows with Python 3.12.

## Strategy agreed for this project

- Supported underlyings: **NIFTY and SENSEX**. Select an expiry from the broker before starting; expiry selection is explicit, not inferred from a historical screenshot.
- Sell one ATM CE and one ATM PE at **09:18, 09:45, 10:15, 10:45, 11:15, 11:45, 12:15, 12:45, 13:15, 13:45, 14:15 and 14:45 IST**. Twelve straddles per underlying if every entry succeeds. Each underlying is started separately.
- ATM is the fresh index price rounded to the nearest strike step (NIFTY 50; SENSEX 100; ties round upward). If outside the loaded chain, fetch and subscribe to a refreshed chain. Skip entries without fresh index and option prices. No replay of entries missed before start or while disconnected.
- **Dry startup test:** after fresh broker login and the first chain load, a new dry session automatically sells its first ATM straddle on fresh index/option ticks during 09:15–15:10 IST on weekdays. It uses the first 20% leg stops, records its actual entry time (marked `startup`), and consumes the 09:18 tranche. Only remaining future scheduled times run afterward. Reloading the page, reloading a chain, or restarting cannot duplicate this first entry. Recovered/paused sessions require **Start dry TBS** to resume. Manually starting a new dry broker session also enters its first straddle immediately. Live entries retain the agreed schedule and confirmations.
- One lot per leg: **NIFTY 65**, **SENSEX 20**. The broker's current lot metadata must match. A mismatch blocks new entries instead of silently changing size.
- First tranche: **20% SL on each leg's actual fill price**. Subsequent tranches: **30% SL**. Buy stop-limit trigger = entry × (1 + SL%). Limit = trigger × **1.02**. Both round upward to broker tick size. Each leg is handled independently; no re-entry after SL.
- Entry and scheduled exit orders are **MIS / DAY market orders**. Protective orders are **MIS / DAY stop-limit orders** at the broker in live mode. The terminal waits for a confirmed fill and confirmed protection on the first leg before selling the second. It is not an atomic multi-leg order.
- Exit all remaining tracked legs at **15:10 IST**. Cancel protective stops and wait for confirmation before sending buy-to-close orders for the remaining confirmed quantity. A partial stop fill reduces the closing quantity.
- **No daily loss cutoff**, as requested. No automatic breakeven move, trailing, or profit-booking logic was specified.
- The manual ticket is a separate tool and uses limit orders. Its positions are not automatically adopted by TBS or covered by TBS stop-losses.

## Dry and live modes

1. **Explore demo** creates visibly labelled synthetic prices; it does not contact the broker. Start dry TBS, then use **Demo: next tranche** to step through the twelve entries. Demo time is manually advanced, so it works outside market hours. Use **Exit TBS** to close the demo session. Demo quantities are 65/20 for this strategy, not authoritative contract specifications.
2. **Connect broker** authenticates through TOTP then MPIN. Choose an expiry to load the chain. **Dry** uses actual WebSocket quotes but records fills only in the local ledger. The first loaded chain starts a new dry startup test automatically; other underlyings are started separately with **Start dry TBS**.
3. Dry fills use 0.05% adverse simulated slippage, rounded to tick size. Marketable limit orders only in the manual ticket. Simulated stop-limits trigger on an observed LTP and fill only when the simulated buy price is within their limit. Fees, brokerage, taxes, depth, margin, queues, and market impact are not modeled.
4. For live operation, select **Live**, choose **Arm live**, type `ENABLE LIVE`, then **Start live TBS** and type `START LIVE TBS`. This explicitly starts automatic real orders for the selected underlying and expiry. Manual live orders separately require `PLACE LIVE ORDER`.
5. **Pause entries** stops new tranches but continues protection and exits. **Lock orders** also blocks new manual and automatic entries; it does not cancel orders or flatten positions. **Exit TBS** closes strategy-owned legs; live confirmation is `EXIT LIVE TBS`. It does not close unrelated account positions.

## Heatmap

The intraday P&L chart shows today's terminal-recorded fills for the selected underlying and execution mode, including manual orders and TBS. Use Overall, CE or PE to filter, and toggle realized/unrealized lines. Hover for time and values. Green/red totals and shading show profit/loss in rupees before fees. Open counts refer to contracts with remaining quantity.

Observed history is saved every five seconds in the ignored local ledger and retained for 30 days. Paper, live, demo, underlying and account histories are separate. New orders record account and option-type metadata; older untagged orders use their existing contract metadata/symbol and remain included as legacy terminal fills because their account cannot be determined. No chart history is fabricated for periods before this feature starts. Stale or missing marks and recording interruptions appear as gaps. Live values use confirmed terminal order fills, including confirmed partial fills; external broker trades and overnight positions are outside this intraday chart.

Rows 10–100% are counterfactual per-leg stop-loss scenarios for each actual entry. Each scenario uses the 2% stop-limit buffer and observed ticks; it latches a trigger, tracks an unfilled limit after a gap, and freezes its exit on a fill. It continues after the actual strategy exits via a stop and closes at session exit. Numbers are **combined CE+PE premium points**. Actual P&L is rupees using leg quantities.

**Best SL is hindsight analytics only** and is never used to choose the strategy's stop. A feed outage can miss a threshold crossing; this is not an exchange-tick backtest. No prices or strategy results are invented when the broker feed is absent. A stale position is marked unavailable instead of being represented as current P&L.

## Credentials and state

The connection form takes a consumer key, registered mobile number with country code, UCC, MPIN, and current six-digit TOTP. Optional saved credentials are encrypted with **Windows DPAPI for the current Windows user**, in `.local/credentials.dpapi`. TOTP and broker session tokens are never saved. Use **Forget saved credentials** to remove the encrypted file. SDK file logging is disabled.

Orders and strategy state are stored in `.local/terminal.sqlite3`, excluded from Git. Intent is committed before an order request. An uncertain response is **not retried automatically**; new live entry is disarmed until broker reconciliation resolves it. The order stream and a 10-second REST reconciliation track fills, partial fills, cancellations and rejections. Download the ledger from **Order book → Export CSV**.

## Operational limits

- **Keep the computer awake and the terminal process running** for scheduled actions and the 15:10 exit. Broker-native stops remain at the broker while the terminal is offline; the timed exit does not run without this process. If a stop-limit gaps beyond its limit, it can remain unfilled.
- Restart pauses recovered sessions and disarms live entry. Reconnect the same account, select the recovered execution mode and verify reconciliation. Saved strategy legs are restored for management; a new dry session can start automatically after the first chain load. Do not switch accounts with open strategy legs.
- Each new leg must have a premium notional at most **₹25,000**, and one lot is enforced. This is an order-size cap, not a portfolio exposure or margin limit. Broker margin rejections are possible. A broker-rejected protective stop triggers a close of confirmed strategy fills; unknown outcomes require manual reconciliation.
- Live API execution, account-specific index naming, tick/lot metadata, stop-order acceptance and actual fills require validation with the user's broker credentials. Automated tests use fake broker responses and never place a real trade.
- This version has no exchange holiday calendar. It requires fresh live index and option ticks and only schedules broker entries on weekdays during the configured session. Expired/unavailable contracts fail closed.
- Use the same broker account across restarts. Broker reports remain the source of truth. Terminal reconciliation matches saved broker IDs or order tags; an unmatched uncertain order requires checking directly in the broker, not resubmission.

## Immediate paper test monitor

`scripts/paper_now.py` is an optional helper for already-filled, one-lot manual paper legs. It reads an explicitly supplied job file under the ignored `.local` folder, manages each simulated stop-limit, and closes remaining legs at the job's exit time. It does not start automatically, enter new trades, or add manual positions to the TBS heatmap. The normal daily launcher uses the TBS schedule above.

Keep the terminal and helper running with broker data connected, Dry mode selected, and manual orders unlocked. Stale quotes delay execution; gaps beyond a stop limit wait for a pullback. The helper saves errors and confirmed closes in its job file and reuses a persisted order ID after an uncertain paper response to prevent duplicate closes.

## Development

Python 3.12, FastAPI, Uvicorn, plain HTML/CSS/JavaScript. No frontend build step. `requirements.txt` includes the exact tested dependency versions in `requirements.lock`. The vendored SDK is unchanged, with upstream attribution and license alongside it.

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765 --no-access-log
```

For an isolated smoke test, run `Start-OptionsDashboard.ps1 -Port 8766 -NoBrowser -DataDirectory <temporary-folder>`. Stop that test instance with `Stop-OptionsDashboard.ps1 -Port 8766`. Do not authenticate a test instance alongside an active trading session. `NEO_SDK_PATH` can override the bundled SDK for development.

## Daily Performance

Net, realized and unrealized P&L cards and the order book reset by IST calendar day without deleting fills. The Daily Performance tab preserves daily results by market, mode and feed in the ignored local ledger, beyond the chart's 30-day retention. Closed legacy days are recovered from recorded fills; historical open days use saved observations or show unavailable marks. Positions and protection still retain prior open quantities; a date reset never closes or deletes positions. Live metrics cover terminal-confirmed fills only.

## Linux server and remote access

See [Ubuntu deployment instructions](deploy/README.md). The server can be accessed through its public IP with a trusted HTTPS certificate and a separate dashboard login. A single worker binds to loopback behind an authenticated Nginx proxy. Linux credential storage uses an owner-only Fernet key; Windows DPAPI credentials cannot be migrated directly. Enter broker credentials anew. Server restarts begin in dry mode with live entry disarmed.

### Broker terminal

Open **Broker terminal** after connecting Kotak Neo to view available trading funds, margin used, collateral, holdings value and P&L, and all broker positions including trades placed outside OptionsDashboard. Funds and holdings refresh every 30 seconds while the tab is open; **Refresh account** requests a new snapshot immediately. Positions use the existing 10-second reconciliation. Unavailable values display as a dash, and failed refreshes retain the previous snapshot with an error and its original timestamp.

Create named watchlists, select an exchange, and search for instruments to add. You can rename lists and remove instruments or lists. Each broker account can save up to 10 lists of 50 instruments in the existing terminal database. Watchlists survive restarts; prices refresh from broker REST quotes and are marked when unavailable or stale. The account overview always displays broker data independently of paper/live execution mode.
