# Wall Street Council

A panel of AI models that argue about which US stock to buy, live, where you can watch.
Gemini, a bench of NVIDIA-hosted models, and Codex (plus Claude, if you seat it) each take a
different analyst role, read the same research brief, pitch, cross-examine each other by name,
vote, and then a chair rules. It ships as an **MCP server** and a **live web monitor**.

> **Paper trading only.** Nothing here connects to a broker or places an order. The output is
> model opinion, not financial advice. Stock returns over days or weeks are mostly noise, and no
> number of models removes that. The quant check at the end of every council says so in numbers.

## The table

| Seat | Default model | Role |
|---|---|---|
| Gemini | `gemini-3.5-flash` (Google AI Studio) | Macro strategist: Fed, rates, dollar, oil, rotation |
| Nemotron Super | `nvidia/nemotron-3-super-120b-a12b` | Quant technician: trend, RSI, moving averages, volume, timing |
| Kimi K3 | `moonshotai/kimi-k3` | Fundamentals: valuation, growth, margins, analyst targets, earnings dates |
| GPT-OSS | `openai/gpt-oss-20b` | News and sentiment |
| Nemotron Ultra | `nvidia/nemotron-3-ultra-550b-a55b` | Risk manager, argues for CASH when warranted |
| Muse | `meta/muse-glimmer-30b` | Alternative data: sports, entertainment, consumer and social trends |
| GLM | `z-ai/glm-5.3` | Contrarian: mean reversion, fading hype |
| Codex | local Codex CLI (ChatGPT sign-in) | Chair: weighs the arguments and rules |
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
- **Full councils** at 09:05 and 15:30 Eastern on weekdays run the whole protocol.
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
| `--council-times` | 09:05,15:30 | Eastern times for full councils |

```bash
uv run council live            # foreground; Ctrl-C to stop
uv run council budget          # ChatGPT and Claude calls used and left today
uv run council stop-live       # stop a detached floor (started from the MCP tool)
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
uv run council monitor                         # live monitor at http://127.0.0.1:8765
uv run council run --budget 10 --rounds 1      # a council in the terminal, visible in the monitor too
uv run council run --tickers DKNG,NKE --claude # add names, seat Claude
uv run council portfolio
uv run council leaderboard
```

The monitor can also convene a council from its sidebar.

### As an MCP server

```bash
claude mcp add wallstreet-council -- uv run --project /path/to/wallstreet-council council mcp
codex mcp add wallstreet-council -- uv run --project /path/to/wallstreet-council council mcp
```

The server starts the monitor on port 8765 in the background. Tools:

| Tool | What it does |
|---|---|
| `convene_council` | start a debate (budget, horizon, extra tickers, rounds, seat Claude); returns session id and monitor URL |
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

## Data

Everything lives in `~/.wallstreet-council/council.db` (SQLite; override with `COUNCIL_DB`).

## Limits worth knowing

- US market holidays are not modelled in the trading-day count.
- Headlines are titles only, and VADER is a blunt instrument. The models do the real reading.
- `yfinance` is an unofficial Yahoo client and can break or rate-limit.
- A "month end" horizon near the end of a month is a day or two. Set `--horizon` to something honest.
