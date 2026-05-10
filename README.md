# 0DTE SPX Iron Condor Alerter

Periodic Telegram alerts for a defined-risk 0DTE SPX iron condor strategy, deployed via GitHub Actions.

> ⚠️ **Disclaimer.** This is a research / personal-use tool. It is **not** investment advice and it does **not** place trades. It only sends signal alerts to your Telegram chat. You make every trading decision yourself.

---

## What you get

Three scheduled alerts per market day:

| Time (ET) | Alert | What it sends |
|---|---|---|
| **09:32** | Morning open | Trade plan: strikes, estimated credit, regime status. Or "SKIP" with reason. |
| **10:00 → 15:55** every 15 min | Intraday monitor (silent unless triggered) | 50% PT hit · 150% loss heads-up · Short-strike breach |
| **16:05** | EOD recap | SPX close, settlement P&L, win/loss outcome |

The strategy that drives them is the same one validated in the `0dte_research/` backtest (~84% win rate, Sharpe ~3, 2-yr study).

---

## Architecture

```
GitHub Actions (cron)  ──▶  Python script  ──▶  Tradier (market data)
                                  │
                                  ├──▶  Telegram Bot API  (alerts)
                                  └──▶  state/today.json  (committed back to repo)
```

State lives in `state/today.json`, committed by the morning workflow and read by the intraday + EOD workflows. `state/history.jsonl` accumulates an audit trail of every day's plan + outcome.

---

## Setup (one-time)

### 1. Get a Tradier production token

1. Open a Tradier brokerage account at https://tradier.com (free).
2. In the dashboard → **API Access** → copy your production access token.
3. Production base URL is `https://api.tradier.com`.

(Sandbox tokens at `https://sandbox.tradier.com` work too, but equity quotes are 15-min delayed — fine for daily alerts, suboptimal for intraday breach detection.)

### 2. You already have a Telegram bot

You'll need:

- `TELEGRAM_BOT_TOKEN` (the long string from @BotFather)
- `TELEGRAM_CHAT_ID` (the chat ID where you want alerts delivered)

If you don't remember your chat ID: send any message to your bot, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id` from the response.

### 3. Create the GitHub repo

```bash
cd 0dte-spx-alerts/
git init -b main
git add .
git commit -m "initial commit"
gh repo create 0dte-spx-alerts --private --source . --push
# or push to a manually-created repo
```

### 4. Add four GitHub Secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Name | Value |
|---|---|
| `TRADIER_TOKEN` | your Tradier access token |
| `TRADIER_BASE_URL` | `https://api.tradier.com` |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | the numeric chat ID |

The default `GITHUB_TOKEN` is automatically granted `contents: write` permission via the workflow file — no additional PAT needed for state commits.

### 5. Enable Actions

Repo → **Actions** tab → **I understand my workflows, go ahead and enable them**.

That's it. The next 09:32 ET on a weekday, you'll get your first morning alert.

### 6. Test before waiting

You can trigger any workflow manually:

Actions tab → pick **0DTE Morning Alert** → **Run workflow** button. Note the script self-checks the local ET time and exits early if you trigger it at, say, 3am ET, so to actually exercise it end-to-end you'll need to either run it during the live window, or comment out the `within_window(...)` check temporarily.

---

## Local development

Quick loop without burning Actions minutes:

```bash
cp .env.example .env
# Edit .env with your real tokens
pip install -r requirements.txt
make test                    # runs pytest suite
make morning-dry             # pulls live data, prints would-send Telegram, no commit
```

`DRY_RUN=1` (set by `make *-dry`) tells the script to log the Telegram message instead of sending it, and skip the git commit.

---

## Configuration

Tunable parameters live in `config.yaml`:

| Section | Key | Default | Meaning |
|---|---|---|---|
| `strategy` | `sigma_multiplier` | 1.0 | Strikes at +/- (mult × Expected Move) |
| `strategy` | `wing_width` | 25 | SPX points; defines max loss |
| `strategy` | `em_multiplier` | 0.85 | tastytrade EM = 0.85 × ATM straddle |
| `filters` | `vol_skip_threshold` | 25.0 | Skip when annualized RV > this |
| `filters` | `overnight_gap_threshold` | 0.0075 | Skip when \|gap\| > 0.75% |
| `filters` | `trend_skip_threshold` | 0.040 | Skip when \|5d return\| > 4% |
| `intraday` | `pt_fraction` | 0.50 | Fire 50% PT alert at this fraction of credit |
| `intraday` | `loss_warning_multiple` | 1.50 | Fire loss heads-up at 150% of credit |

Push to main and the next workflow run uses the new values.

---

## File structure

```
0dte-spx-alerts/
├── .github/workflows/
│   ├── morning_alert.yml          # cron 09:32 ET (handles DST automatically)
│   ├── intraday_monitor.yml       # cron */15 during 10:00-15:55 ET
│   └── eod_recap.yml              # cron 16:05 ET
├── src/
│   ├── morning.py                 # entry point: open alert
│   ├── intraday.py                # entry point: breach + PT/loss monitor
│   ├── eod.py                     # entry point: EOD recap
│   ├── tradier.py                 # REST client
│   ├── strategy.py                # filters + strike selection
│   ├── pricing.py                 # Black-Scholes + iron-condor math
│   ├── telegram_bot.py            # message sender
│   ├── state.py                   # state.json read/write/commit
│   ├── timecheck.py               # ET-aware time gating
│   └── config.py                  # YAML + env loader
├── tests/
│   ├── test_strategy.py
│   └── test_pricing.py
├── state/
│   ├── today.json                 # current day's plan (committed daily)
│   └── history.jsonl              # append-only audit log
├── config.yaml                    # tunable parameters
├── requirements.txt
├── Makefile
├── .env.example
├── .gitignore
└── README.md
```

---

## Strategy summary (for reference)

- **Universe.** SPX, cash-settled European, daily SPXW expiries.
- **Edge.** VRP capture + 0DTE theta + intraday mean-reversion in positive-gamma regime.
- **Trade.** Sell iron condor at 09:32 ET. Short strikes at ±1 expected move (EM = 0.85 × ATM straddle). 25-pt wings.
- **Filters.** Skip if (i) realized vol > 25 annualized, (ii) overnight gap > 0.75%, or (iii) 5-day return > 4%.
- **Exit.** Hold to 16:00 ET cash settlement. Optional intraday alerts at 50% PT and 150% loss heads-up.
- **Sizing.** Always 1 contract in the alert. You decide actual size manually.
- **Backtest.** 2024-05 → 2026-05 (501 sessions, 386 active trades). 83.7% win rate, Sharpe 3.15, max DD 13.6%, profit factor 1.88.

See `../0dte_research/REPORT.md` for the full backtest writeup.

---

## Operational notes

- **Cron jitter.** GitHub Actions can fire 5-15 min late. Each script self-checks the local ET time and exits silently if it's outside the configured tolerance window (12 min by default).
- **DST.** Both DST and EST cron schedules are listed in each workflow. The local ET self-check ensures only the in-window run does work; the other exits in <1 second.
- **Holidays.** The morning script calls Tradier's `/markets/clock` and exits if `state == "closed"`.
- **Failures.** If Tradier or Telegram is down, the workflow fails (GitHub will email you). The state file is not corrupted because we only write on success.
- **Action minutes.** Total: ~1 morning + ~24 intraday + ~1 EOD = ~26 runs/day × ~1 min each × ~21 trading days/month ≈ 550 min/month, well within the free 2000-min tier.
- **Duplicate alerts.** Each intraday alert (PT, loss, breach) fires at most once per day; the `alerts_fired` flags in state prevent repeats.

---

## License

MIT. Use at your own risk. Past backtest performance does not guarantee future results.
