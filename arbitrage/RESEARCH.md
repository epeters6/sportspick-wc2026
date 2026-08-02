# API Research — Kalshi & Polymarket Arbitrage Scanner

> **Researched**: 2026-07-15
> **Status**: Verified against official documentation (docs.kalshi.com, docs.polymarket.com)

---

## 1. Kalshi Public API

### Base URL
- **Production**: `https://external-api.kalshi.com/trade-api/v2`
- **Demo/Sandbox**: `https://external-api.demo.kalshi.co/trade-api/v2`

### Authentication for Read-Only Data
**Not required.** All market data endpoints (series, events, markets, orderbook) are fully public and do not require API keys or authentication headers.

### Key Endpoints (Public, No Auth)

| Endpoint | Description |
|----------|-------------|
| `GET /series/{series_ticker}` | Series metadata (title, category, frequency) |
| `GET /events` | List all events (paginated, filterable) |
| `GET /events/{event_ticker}` | Single event details |
| `GET /markets` | List markets with filters (series_ticker, status, etc.) |
| `GET /markets/{ticker}` | Single market details |
| `GET /markets/{ticker}/orderbook` | Live orderbook (yes bids, no bids) |

### Key Market Response Fields
```json
{
  "ticker": "KXHIGHNY-26JUL15-T93",
  "event_ticker": "KXHIGHNY-26JUL15",
  "title": "Highest temperature in NYC 93° or above?",
  "yes_bid_dollars": "0.65",
  "yes_ask_dollars": "0.68",
  "no_bid_dollars": "0.32",
  "no_ask_dollars": "0.35",
  "volume_fp": "12345",
  "status": "open",
  "close_time": "2026-07-15T23:59:59Z",
  "expiration_time": "2026-07-16T12:00:00Z"
}
```

### Price Format
- Prices are in **dollars** (0.00 to 1.00), representing probability
- To convert to cents: multiply by 100
- `yes_bid_dollars` = best bid for YES contracts
- `yes_ask_dollars` = best ask for YES contracts
- In binary markets: YES ask ≈ (1 - NO bid), so the API returns bids only for both sides

### Pagination
- Cursor-based pagination using `cursor` parameter
- Returns `cursor` in response for next page

### Rate Limits
- Token-bucket based rate limiting
- Unauthenticated: subject to basic rate limits (~10 req/s estimated)
- Returns HTTP 429 on rate limit exceeded
- Implement exponential backoff

### Filtering Markets
```
GET /markets?status=open                    # Only open markets
GET /markets?series_ticker=KXHIGHNY         # By series
GET /markets?event_ticker=KXHIGHNY-26JUL15  # By event
```

---

## 2. Polymarket Public API

### Base URLs
- **Gamma API** (market discovery/metadata): `https://gamma-api.polymarket.com`
- **CLOB API** (prices/orderbook): `https://clob.polymarket.com`

### Authentication for Read-Only Data
**Not required.** Both Gamma API and CLOB read-only endpoints are fully public.

### Gamma API — Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /events` | List events with filtering and pagination |
| `GET /events/{id}` | Single event by ID |
| `GET /events/slug/{slug}` | Single event by URL slug |
| `GET /markets` | List markets with filtering and pagination |
| `GET /markets/{id}` | Single market by ID |
| `GET /public-search` | Full-text search across events/markets |
| `GET /tags` | Available tags/categories |

### Gamma API Key Parameters for Events
```
GET /events?active=true&closed=false&limit=100         # All active events
GET /events?active=true&closed=false&order=volume_24hr  # Sorted by volume
GET /events?slug=fed-decision-in-october                # By slug
GET /events?tag_id=100381                               # By category tag
```

### Gamma API Market Response Fields
```json
{
  "id": "12345",
  "question": "Will Bitcoin reach $100k by end of 2026?",
  "conditionId": "0xabc...",
  "slug": "will-bitcoin-reach-100k",
  "outcomes": "[\"Yes\", \"No\"]",
  "outcomePrices": "[\"0.65\", \"0.35\"]",
  "volume": "1500000",
  "volume24hr": 50000.0,
  "liquidity": "25000",
  "endDate": "2026-12-31T23:59:59Z",
  "active": true,
  "closed": false,
  "enableOrderBook": true,
  "clobTokenIds": "[\"token_yes_id\", \"token_no_id\"]"
}
```

### CLOB API — Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /price?token_id={id}&side=buy` | Current price for a token |
| `GET /midpoint?token_id={id}` | Midpoint price |
| `GET /book?token_id={id}` | Full orderbook |
| `GET /spread?token_id={id}` | Bid-ask spread |

### Price Format
- Prices are **decimals from 0 to 1** representing probability
- `outcomePrices` in the Gamma response: `"0.65"` means 65 cents
- To convert to cents: multiply by 100

### Pagination (Gamma API)
- Offset-based: `limit` and `offset` parameters
- Also supports keyset pagination via `after_cursor`

### Rate Limits
- Public endpoints: ~20 requests/second
- Returns HTTP 429 on rate limit exceeded

---

## 3. Cross-Platform Matching Strategy

### Challenge
Kalshi and Polymarket use different naming conventions for the same events:
- **Kalshi**: Uses series tickers and structured market titles (e.g., "Will the Fed cut rates at its July 2026 meeting?")
- **Polymarket**: Uses event questions/slugs (e.g., "Fed Rate Cut in July 2026?")

### Approach — Multi-Signal Fuzzy Matching

1. **Fetch all active events** from both platforms
2. **Normalize titles**: lowercase, strip punctuation, remove common stop words ("will", "the", "?", etc.)
3. **Extract key entities**: dates, names, numbers, categories
4. **Fuzzy string similarity**: Use `rapidfuzz` (Python) for token-set-ratio matching
5. **Date matching**: Compare `close_time` (Kalshi) with `endDate` (Polymarket) — require within ±24h
6. **Confidence scoring**: Combine title similarity + date proximity into a confidence score
7. **Threshold**: Auto-include at ≥80% confidence; flag for manual review at 50-80%; reject below 50%

### Known Overlap Categories
Both platforms tend to list markets in these categories:
- **Federal Reserve / Interest Rates** (most overlap)
- **Elections / Politics**
- **Economic indicators** (GDP, CPI, Jobs numbers)
- **Crypto prices**
- **Sports outcomes**
- **Weather** (Kalshi is dominant here)

---

## 4. Arbitrage Math

### Formula
Given a YES/NO contract pair for the same event:
- Kalshi YES price: `K_yes` (in cents)
- Polymarket YES price: `P_yes` (in cents)

**If `P_yes > K_yes`** (Polymarket prices YES higher):
- Buy YES on Kalshi at `K_yes` cents
- Buy NO on Polymarket at `(100 - P_yes)` cents
- Total cost = `K_yes + (100 - P_yes)` cents
- Guaranteed payout = 100 cents (one side always wins)
- **Gross profit = `P_yes - K_yes`** cents per contract pair
- **Net profit = Gross profit - fees**

**If `K_yes > P_yes`** (Kalshi prices YES higher):
- Buy YES on Polymarket at `P_yes` cents
- Buy NO on Kalshi at `(100 - K_yes)` cents
- Total cost = `P_yes + (100 - K_yes)` cents
- **Gross profit = `K_yes - P_yes`** cents per contract pair

### Return on Capital
- Capital required = total cost per contract pair
- ROI% = (Net profit / Capital required) × 100

### Fee Assumptions (Configurable)
- **Kalshi**: ~2-7 cents per contract (varies by market and tier)
- **Polymarket**: ~1-2% of trade value (varies by maker/taker)
- Default assumption in app: **3 cents per contract** on each side = 6 cents total

---

## 5. Implementation Notes

### Why Python/FastAPI Stack
- Both APIs are simple REST with JSON — Python `httpx`/`aiohttp` handles this cleanly
- `rapidfuzz` is the best fuzzy matching library (Python-only, C-backed, very fast)
- `sqlite3` is built into Python stdlib for the history database
- FastAPI gives us auto-generated API docs and async support
- Plain HTML/JS frontend avoids build tool complexity

### Refresh Strategy
- Poll both APIs every N seconds (default: 30)
- Stagger requests to avoid hitting rate limits
- Cache events for 5 minutes (they don't change often)
- Only re-fetch prices on each cycle (they change frequently)
