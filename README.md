# Wall Street Council

A panel of AI models that argue, around the clock and in public, about US and Indian stocks, IPOs and any
question you bring. Gemini, a bench of NVIDIA-hosted models and Codex (plus Claude, if you seat it) each
take an analyst role, read the same research, pitch, cross-examine each other by name, vote, and a chair
rules. Every call is graded against the index afterwards, and the council learns from the results.
It ships as an **MCP server**, a **live local monitor**, and a **read-only public site** on GitHub Pages.

> **Paper trading only.** Nothing here connects to a broker or places an order. The output is model
> opinion, not financial advice. Short-horizon returns are mostly noise, and costs and taxes here are
> approximate published schedules, not advice for your situation.

## The goal, and the daily pipeline

**Goal:** grow $10 (and Rs 1,000) toward $100 with AI picks a non-trader can follow, judged honestly
against just buying the index. Every feature is kept only if it serves that.

1. **Collect** prices, full history, good and bad news, broker charges and taxes.
2. **Shortlist** only what the budget can buy: any US stock (fractional shares), Indian stocks under Rs 1,000.
3. **Debate** all day on free-tier models; full councils twice a day judged by Codex (US) and Copilot (India),
   two calls a day each.
4. **Decide** one Pick of the Day per market: BUY or WAIT, three plain reasons, good and bad news, and a backup
   pick for anyone who wants to buy anyway.
5. **Record and grade** every pick against the S&P 500 or Nifty 50.
6. **Learn**: lessons from graded picks, more weight to the seats that were right.
7. **Publish** a page a child can read: a BUY or WAIT card per market, a one-year chart against the market,
   "$10 a year ago would be $X", good news and bad news, the stock's story, and a robots-vs-market score.

**Deliberately cut or frozen because they drift from the goal:** daily IPO debates (now weekly; Rs 1,000 cannot
buy a lot), a spotlight over unaffordable stocks, a general sports news feed, the Claude seat, options or
leverage tooling, real-money order placement before a long track record, and charts for every stock.

## What it covers

- **Markets**: US (NYSE, Nasdaq) and India (NSE, BSE): indices, VIX and India VIX, yields, USD/INR,
  crude, sectors, today's movers, and a core watchlist in each.
- **Every pitched stock gets its full file**: the entire price history (back to listing), CAGR over
  1/3/5/10 years, worst drawdowns, calendar-year returns, beta and excess return against the index,
  quarterly revenue, profit and margins, earnings surprises and next-day reactions, analyst up and
  downgrades, insider buying and selling, top institutions, dividends and splits.
- **IPOs**: NSE's live issues with subscription multiples, upcoming issues, and how recent listings have
  done since; Nasdaq's upcoming, priced and filed deals; subscription, GMP and listing headlines. Each
  IPO call says whether the budget can afford a lot, what to do if allotted, and what to do if not.
- **Costs and taxes**: broker charges, depository (DP) charges, STT, stamp duty, GST, exchange and SEBI
  fees for Indian delivery and intraday trades, SEC and FINRA fees in the US, and capital-gains tax
  rules for both (with the caveat that residency, for example NRI or F-1/OPT, changes them). On a
  Rs 1,000 delivery trade the round trip costs about 1.8%, mostly the flat DP charge.
- **The goal**: by default the client wants 10x the budget ($10 to $100). The council must weigh every
  route (compounding, concentrated stocks, IPOs, intraday, options, penny stocks) with honest odds and
  show the arithmetic: 10x needs 900% in a month, 21% a month for a year, or about 20 years at 12%.
- **Your chat**: messages you post (monitor chat box, `council say`, MCP `say_to_floor`) wake the floor at
  once, and every council reads your recent messages as CLIENT NOTES.
- **A trader's playbook** (`playbook.md`): base rates, momentum, reversal, post-earnings drift, risk rules,
  IPO and India specifics, given to every seat.

## How it learns

The models' weights never change; what changes is what they are told and how much they are trusted.

1. Every vote and ruling is recorded with the stock price **and the index level** at that moment.
2. Calls are graded hourly by **excess return**: the pick's return minus the index's over the same span.
   CASH is a call too. A call becomes final at its horizon.
3. Each seat's final record becomes a **trust weight** (shrunk toward 1.0 until it has a sample) that
   multiplies its votes in the tally.
4. Once a day, final calls and the reasons given for them go to a free-tier model that writes short
   **lessons**; they are fed back into every seat's prompt, along with that seat's own record.

## Why anyone should (or should not) trust it

The public site shows the **track record**: every ruling, its return, what the index did meanwhile, and
whether it beat it, losses included. Snapshots are ordinary commits on the `gh-pages` branch, so the
branch history is a timestamped public ledger: each ruling is there before its outcome is known. Until
that record covers months and beats the index after costs, it is an experiment.

## The table

| Seat | Default model | Role |
|---|---|---|
| Gemini | `gemini-3.5-flash` (Google AI Studio) | Macro strategist: Fed, RBI, rates, currencies, oil, flows |
| Nemotron Super | `nvidia/nemotron-3-super-120b-a12b` | Quant technician: trend, RSI, moving averages, volume, timing |
| Kimi K3 | `moonshotai/kimi-k3` | Fundamentals: valuation, growth, margins, analyst targets, earnings dates |
| GPT-OSS | `openai/gpt-oss-20b` | News and sentiment |
| Nemotron Ultra | `nvidia/nemotron-3-ultra-550b-a55b` | Risk manager, argues for CASH when warranted |
| Muse | `meta/muse-glimmer-30b` | Alternative data: IPL and cricket, NFL, entertainment, festivals, consumer trends |
| GLM | `z-ai/glm-5.3` | Contrarian: mean reversion, fading hype |
| Codex | local Codex CLI (ChatGPT sign-in) | Chair for US councils |
| Copilot | local GitHub Copilot CLI (GitHub sign-in) | Chair for Indian and IPO councils |
| Claude (optional) | local Claude Code CLI, Opus | Devil's advocate |

Every seat has fallbacks, and busy endpoints (429, 503) are retried. Change the table by writing
`~/.wallstreet-council/seats.json` (same shape as `DEFAULT_SEATS` in `council.py`).

## What they read

The research desk builds one brief per council from free sources, no paid keys:

- **Market regime**: S&P 500, Nasdaq 100, Russell 2000, VIX, 10-year yield, dollar, oil, gold, bitcoin
- **Sector rotation**: 11 SPDR sectors plus semis, 1 day, 5 days, 1 month
- **Candidates**: today's most active, top gainers and top losers (market cap at least $2B, price at least $5), a core watchlist, and any tickers you add
- **Per candidate**: 1-year history (returns over 1d to 1y, RSI 14, distance from 20/50/200-day averages, realized volatility, volume surge, 52-week position), fundamentals (P/E, growth, margins, analyst target, next earnings date, short interest), and the last two days of headlines with VADER sentiment
- **Macro and policy headlines**: Fed, inflation and jobs, tariffs, earnings, geopolitics
- **Sports and culture headlines**: sports business, betting, media rights, big events

Prices and history come from Yahoo Finance through `yfinance`; headlines from Google News RSS.

## The protocol

1. **Research**: the desk builds the brief.
2. **Opening**: every seat pitches up to two picks with thesis, catalysts, risks, an entry window in Eastern time, and a bear, base and bull month-end return.
3. **Cross-examination**: every seat reads the table, challenges the weakest argument and backs the strongest, by name. Repeat with `--rounds`.
4. **Vote**: one ticker or CASH per seat, with confidence.
5. **Ruling**: the chair sees everything plus a confidence-weighted tally and rules, naming the dissent.
6. **Quant check**: the one-and two-standard-deviation band for your budget at month end, from the pick's realized volatility. Computed, not guessed.
7. **Paper position**: opened at the live price and marked to market from then on. A **seat leaderboard** scores every seat's votes against what the stock actually did, so over time you learn which model is worth listening to.

## Talking all day on a small ChatGPT budget

`council live` keeps the council talking without burning your ChatGPT plan:

- **Floor chatter** every 15 minutes while the market is open (every 60 when closed): three seats from
  the free-tier bench (NVIDIA, Gemini) take turns reacting to the fresh tape, today's movers and new
  headlines, and to each other by name. One rolling conversation per day, shown in the monitor as
  "Trading floor".
- **Full councils** on a schedule, each time in its market's zone, weekdays (default
  `pick-US@09:05, pick-IN@09:20, ipo-IN@12:30`; change with `--schedule`).
- **A spotlight stock** each round, with its full history, rotating through movers and both watchlists (and
  anything you name in chat first), so over the days the floor works through every name.
- **Codex only chairs full councils**, never floor chatter, with low reasoning effort and a slim brief
  (just the voted tickers and the votes). A hard daily ration, default **2 Codex calls per US Eastern
  day**, is counted in the database; once it is spent the chair's fallback (Nemotron Ultra) rules instead.
  Claude, when seated, has its own ration.

| Setting | Default | Meaning |
|---|---|---|
| `COUNCIL_CODEX_DAILY` | 2 | Codex calls per Eastern day. `0` keeps ChatGPT out entirely |
| `COUNCIL_CLAUDE_DAILY` | 2 | Claude calls per day, only when a council seats Claude |
| `COUNCIL_CODEX_EFFORT` | low | Codex reasoning effort |
| `--interval-open` / `--interval-closed` | 15 / 60 | minutes between floor rounds |
| `--schedule` | pick-US@09:05, pick-IN@09:20, ipo-IN@12:30 | full councils |
| `--goal` | 100 | the client's target, in dollars |

```bash
uv run council live            # foreground; Ctrl-C to stop
uv run council budget          # ChatGPT and Claude calls used and left today
uv run council stop-live       # pause the floor (service or detached); the monitor keeps running
uv run council start-live      # resume it (a paused service also resumes at next login)
```

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/srikar0805/wallstreet-council && cd wallstreet-council
uv sync
```

Keys are read from environment variables, or on macOS from the keychain:

| Provider | Env var | Keychain service |
|---|---|---|
| NVIDIA NIM | `NVIDIA_API_KEY` | `nvidia-api-key` |
| Google AI Studio | `GEMINI_API_KEY` | `gemini-api-key` |
| Codex | none, uses `codex` signed in with ChatGPT (`CODEX_BIN` to override) | |
| Claude | none, uses `claude` signed in with claude.ai (`CLAUDE_BIN` to override) | |

`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are stripped from the Codex and Claude child processes
so neither can silently switch to API billing.

```bash
security add-generic-password -s nvidia-api-key -a "$USER" -w   # prompts for the key
uv run council ping                                              # one tiny call per free-tier seat
uv run council ping --rationed                                   # also Codex/Claude (spends ration)
```

## Use

```bash
uv run council monitor                                  # live monitor at http://127.0.0.1:8765
uv run council run --market US                          # stock pick, $10, goal $100
uv run council run --market IN --budget 1000            # NSE pick, whole shares only
uv run council ipo --market IN --topic "What if I apply for the Srit India IPO and get allotted?"
uv run council debate "Is the Tata Motors demerger good for small holders?" --market IN
uv run council say "What about HDFC Bank after the RBI decision?"   # talk to the live floor
uv run council track                                    # graded track record, seat weights, lessons
uv run council publish                                  # push the public snapshot to GitHub Pages now
uv run council install-service --publish                # macOS: keep floor + monitor + publishing running
uv run council uninstall-service
```

### As an MCP server

```bash
claude mcp add wallstreet-council -- uv run --project /path/to/wallstreet-council council mcp
codex mcp add wallstreet-council -- uv run --project /path/to/wallstreet-council council mcp
```

The server starts the monitor on port 8765 in the background. Tools:

| Tool | What it does |
|---|---|
| `convene_council` | start a debate: mode `pick`, `ipo` or `topic`; market `US`, `IN` or `BOTH`; budget, goal, horizon, tickers, rounds, seat Claude |
| `say_to_floor` | post your message or what-if to the live floor |
| `track_record` | graded rulings against the index, seat trust weights, learned lessons |
| `get_transcript` | messages after `since`; pass `next_since` back to follow live |
| `get_ruling` | the chair's full ruling and the volatility band |
| `list_sessions` | recent councils and their calls |
| `paper_portfolio` | positions marked to market |
| `seat_leaderboard` | which seat's votes have performed best |
| `council_seats` | the current table |
| `monitor_url` | where to watch |
| `start_live` | start the continuous floor in its own process (keeps running after the client exits) |
| `stop_live` | stop it |
| `live_status` | is the floor running, and how much of today's ration is left |

There is deliberately no tool that places an order.

## The public site

`council publish` (or `--publish` on `live` / `install-service`, every 15 minutes) builds a read-only copy of
the monitor with JSON snapshots and commits it to the `gh-pages` branch. Enable Pages once:

```bash
gh api -X POST repos/OWNER/wallstreet-council/pages -f "source[branch]=gh-pages" -f "source[path]=/"
```

The public page has no controls: no convening, no chat. Your own chat messages are shown there; set
`COUNCIL_PUBLISH_CLIENT=0` to leave them out.

## Data

Everything lives in `~/.wallstreet-council/council.db` (SQLite; override with `COUNCIL_DB`). Fee and tax
figures can be overridden in `~/.wallstreet-council/costs.json`.

## Limits worth knowing

- US market holidays are not modelled in the trading-day count.
- Headlines are titles only, and VADER is a blunt instrument. The models do the real reading.
- `yfinance` is an unofficial Yahoo client and can break or rate-limit.
- A "month end" horizon near the end of a month is a day or two. Set `--horizon` to something honest.
