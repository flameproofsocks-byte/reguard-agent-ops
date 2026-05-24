# Reguard Ops

## Project Overview

General-purpose agent dashboard and chatroom for spawning and interacting with AI agents, tracking projects, and staying organized. Also hosts a stock screening project as one of its sub-projects.

---

## Part 1: Backtesting System

Tests trading strategies against historical data for a predefined set of specific stocks over the period **2022–2026**.

**Goals:**
- Validate strategy logic before deploying live
- Measure performance metrics (P&L, win rate, drawdown, etc.)
- Iterate on strategy parameters against known historical data

**Scope:**
- Fixed stock list (to be defined)
- Date range: January 2022 – present (2026)

---

## Part 2: Live Trading System

Two-stage pipeline:

### Stage 1: Scanner

**Filter criteria:**
- Market cap: $10M – $250M
- Stock price: > $1.00
- Intraday gain: > 30%

**Data source:** Insight Sentry Screener API — `POST /v3/screeners/stock`
- `market_cap_min` / `market_cap_max` and `price_min` are confirmed native filter parameters
- `change_percent` is a requestable/sortable field; range filtering (>30%) may be supported via the flexible schema — to be confirmed with live API key testing
- Fallback: fetch sorted by `change_percent desc`, filter client-side if range filter is unsupported
- Results are paginated; must iterate all pages to cover full universe

**Output:** Candidate watchlist passed to Stage 2

### Stage 2: Entry Finder
- Operates on the candidate list produced by the scanner
- Identifies precise entry points for each candidate
- More compute-intensive, focused analysis
- *(Criteria to be defined)*

---

## Data Provider

**Insight Sentry** (`https://insightsentry.com`)
- REST API + WebSocket API
- Screener API available at `/v3/screeners/stock`
- Real-time quotes via WebSocket (useful for entry finder)
- Supports stocks, ETFs, options, futures, crypto, FX across 250+ exchanges

---

## Architecture Notes

*(To be filled in as design decisions are made)*

---

## TODO

- [ ] Define the specific stock list for backtesting
- [ ] Test Insight Sentry screener with live API key — confirm `change_percent` range filter support
- [ ] Define entry criteria (Stage 2)
- [ ] Decide on tech stack
