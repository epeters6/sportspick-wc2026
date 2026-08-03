import "jsr:@supabase/functions-js/edge-runtime.d.ts";

type Obligation = {
  candidate_id: string;
  platform: string | null;
  market_id: string | null;
  outcome_id: string | null;
  side: string | null;
  due_15m: string | null;
  due_1h: string | null;
  due_close: string | null;
  event_start: string | null;
  status_15m: string | null;
  status_1h: string | null;
  status_close: string | null;
  metadata: Record<string, unknown> | null;
};

type Book = {
  price: number | null;
  bookTimestamp: Date | null;
  receivedTimestamp: Date;
};

const OVERDUE_GRACE_MS = 30 * 60 * 1000;
const CLOSE_LEAD_MS = 5 * 60 * 1000;
const BATCH_SIZE = 100;

function parseDate(value: unknown): Date | null {
  if (!value) return null;
  const parsed = new Date(String(value));
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

function kalshiBestAsk(payload: Record<string, unknown>, outcomeId: string): number | null {
  const orderbook = (payload.orderbook_fp ?? payload) as Record<string, unknown>;
  const yes = Array.isArray(orderbook.yes_dollars) ? orderbook.yes_dollars : [];
  const no = Array.isArray(orderbook.no_dollars) ? orderbook.no_dollars : [];
  const bestBid = (levels: unknown[]): number | null => {
    if (!levels.length) return null;
    const level = levels[levels.length - 1];
    if (!Array.isArray(level) || level.length < 2) return null;
    const price = Number(level[0]);
    return Number.isFinite(price) ? price : null;
  };
  const yesBid = bestBid(yes);
  const noBid = bestBid(no);
  // Match KalshiClient exactly: only explicit YES/Y identifiers select YES;
  // every other Kalshi outcome identifier represents the NO contract.
  const buyNo = !["yes", "y"].includes(outcomeId.toLowerCase());
  const oppositeBid = buyNo ? yesBid : noBid;
  if (oppositeBid === null) return null;
  const ask = 1 - oppositeBid;
  return ask > 0 && ask < 1 ? ask : null;
}

async function fetchBook(row: Obligation): Promise<Book> {
  const receivedTimestamp = new Date();
  const platform = String(row.platform ?? "").toLowerCase();
  const marketId = String(row.market_id ?? "");
  const outcomeId = String(row.outcome_id ?? "yes");
  if (!marketId) return { price: null, bookTimestamp: null, receivedTimestamp };

  if (platform === "kalshi") {
    const url = `https://external-api.kalshi.com/trade-api/v2/markets/${encodeURIComponent(marketId)}/orderbook?depth=1`;
    const response = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(`Kalshi book HTTP ${response.status}`);
    const payload = await response.json() as Record<string, unknown>;
    return {
      price: kalshiBestAsk(payload, outcomeId),
      bookTimestamp: null,
      receivedTimestamp,
    };
  }

  if (platform === "polymarket") {
    const url = new URL("https://clob.polymarket.com/book");
    url.searchParams.set("token_id", outcomeId);
    const response = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(`Polymarket book HTTP ${response.status}`);
    const payload = await response.json() as Record<string, unknown>;
    const asks = Array.isArray(payload.asks) ? payload.asks : [];
    const prices = asks
      .map((level) => Number((level as Record<string, unknown>).price))
      .filter((price) => Number.isFinite(price) && price > 0 && price < 1);
    const rawTimestamp = payload.timestamp ?? payload.last_trade_price_timestamp;
    let bookTimestamp: Date | null = null;
    if (rawTimestamp !== undefined && rawTimestamp !== null) {
      const numeric = Number(rawTimestamp);
      if (Number.isFinite(numeric)) {
        bookTimestamp = new Date(numeric > 1e12 ? numeric : numeric * 1000);
      } else {
        bookTimestamp = parseDate(rawTimestamp);
      }
    }
    return {
      price: prices.length ? Math.min(...prices) : null,
      bookTimestamp,
      receivedTimestamp,
    };
  }

  return { price: null, bookTimestamp: null, receivedTimestamp };
}

function checkpointFields(checkpoint: "15m" | "1h" | "close") {
  if (checkpoint === "15m") {
    return ["status_15m", "obs_15m_price", "obs_15m_ts", "book_ts_15m"] as const;
  }
  if (checkpoint === "1h") {
    return ["status_1h", "obs_1h_price", "obs_1h_ts", "book_ts_1h"] as const;
  }
  return ["status_close", "obs_close_price", "obs_close_ts", "book_ts_close"] as const;
}

async function updateRow(
  baseUrl: string,
  serviceKey: string,
  row: Obligation,
  patch: Record<string, unknown>,
  pendingStatuses: string[],
): Promise<boolean> {
  const url = new URL(`${baseUrl}/rest/v1/clv_obligations`);
  url.searchParams.set("candidate_id", `eq.${row.candidate_id}`);
  for (const statusColumn of pendingStatuses) {
    // Compare-and-set: a concurrent GitHub/Supabase collector may have already
    // completed this checkpoint after our initial read.
    url.searchParams.set(statusColumn, "eq.pending");
  }
  const response = await fetch(url, {
    method: "PATCH",
    headers: {
      apikey: serviceKey,
      Authorization: `Bearer ${serviceKey}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
    body: JSON.stringify(patch),
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error(`Supabase patch HTTP ${response.status}`);
  const updated = await response.json() as unknown[];
  return updated.length > 0;
}

function constantTimeEqual(left: string, right: string): boolean {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index % Math.max(a.length, 1)] ?? 0) ^
      (b[index % Math.max(b.length, 1)] ?? 0);
  }
  return difference === 0;
}

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") {
    return new Response(JSON.stringify({ error: "method_not_allowed" }), {
      status: 405,
      headers: { "Content-Type": "application/json" },
    });
  }

  const baseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const schedulerSecret = Deno.env.get("CLV_SCHEDULER_SECRET");
  if (!baseUrl || !serviceKey || !schedulerSecret) {
    return new Response(JSON.stringify({ error: "missing_supabase_environment" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
  const suppliedSecret = request.headers.get("x-clv-scheduler-secret") ?? "";
  if (!constantTimeEqual(suppliedSecret, schedulerSecret)) {
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const now = new Date();
  const nowIso = now.toISOString();
  const query = new URL(`${baseUrl}/rest/v1/clv_obligations`);
  query.searchParams.set("select", "*");
  query.searchParams.set(
    "or",
    `(` +
      `and(status_15m.eq.pending,due_15m.lte.${nowIso}),` +
      `and(status_15m.eq.pending,due_15m.is.null),` +
      `and(status_1h.eq.pending,due_1h.lte.${nowIso}),` +
      `and(status_1h.eq.pending,due_1h.is.null),` +
      `and(status_close.eq.pending,due_close.lte.${nowIso})` +
      `)`,
  );
  query.searchParams.set("order", "updated_at.asc");
  query.searchParams.set("limit", String(BATCH_SIZE));

  const rowsResponse = await fetch(query, {
    headers: {
      apikey: serviceKey,
      Authorization: `Bearer ${serviceKey}`,
    },
    signal: AbortSignal.timeout(8000),
  });
  if (!rowsResponse.ok) {
    return new Response(JSON.stringify({ error: `query_http_${rowsResponse.status}` }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }

  const rows = await rowsResponse.json() as Obligation[];
  const summary = {
    checked: rows.length,
    updated: 0,
    unavailable: 0,
    conflicts: 0,
    errors: 0,
  };

  for (const row of rows) {
    const metadata = { ...(row.metadata ?? {}) } as Record<string, unknown>;
    const patch: Record<string, unknown> = { updated_at: now.toISOString() };
    let touched = false;
    let cachedBook: Book | null | undefined;
    const pendingStatuses: string[] = [];
    let rowUpdated = 0;
    let rowUnavailable = 0;

    for (const checkpoint of ["15m", "1h", "close"] as const) {
      const [statusColumn, priceColumn, observationColumn, bookColumn] =
        checkpointFields(checkpoint);
      if (row[statusColumn] !== "pending") continue;
      const dueValue = checkpoint === "15m"
        ? row.due_15m
        : checkpoint === "1h"
        ? row.due_1h
        : row.due_close;
      let due = parseDate(dueValue);
      if (!due) {
        if (checkpoint === "close") continue;
        patch[statusColumn] = "unavailable";
        patch[observationColumn] = now.toISOString();
        metadata[`${checkpoint}_reason`] = "MISSING_DUE_TIME";
        pendingStatuses.push(statusColumn);
        rowUnavailable += 1;
        touched = true;
        continue;
      }

      let eventStart: Date | null = null;
      if (checkpoint === "close") {
        eventStart = parseDate(metadata.event_start_utc) ?? parseDate(row.event_start);
        if (!eventStart) {
          if (metadata.close_lead_minutes === undefined) {
            eventStart = due;
            due = new Date(eventStart.getTime() - CLOSE_LEAD_MS);
          } else {
            eventStart = new Date(
              due.getTime() + Number(metadata.close_lead_minutes) * 60 * 1000,
            );
          }
        }
      }
      if (now < due) continue;
      if (checkpoint === "close" && eventStart && now >= eventStart) {
        patch[statusColumn] = "unavailable";
        patch[observationColumn] = now.toISOString();
        metadata[`${checkpoint}_reason`] = "POST_START";
        metadata[`${checkpoint}_event_start_utc`] = eventStart.toISOString();
        pendingStatuses.push(statusColumn);
        rowUnavailable += 1;
        touched = true;
        continue;
      }

      const delayMs = now.getTime() - due.getTime();
      if (checkpoint !== "close" && delayMs > OVERDUE_GRACE_MS) {
        patch[statusColumn] = "unavailable";
        patch[observationColumn] = now.toISOString();
        metadata[`${checkpoint}_reason`] = "OBSERVATION_OVERDUE";
        metadata[`${checkpoint}_obs_delay_seconds`] = delayMs / 1000;
        pendingStatuses.push(statusColumn);
        rowUnavailable += 1;
        touched = true;
        continue;
      }

      try {
        if (cachedBook === undefined) cachedBook = await fetchBook(row);
      } catch (error) {
        summary.errors += 1;
        metadata[`${checkpoint}_fetch_error`] = String(error);
        cachedBook = null;
      }
      if (!cachedBook) continue;
      const stamp = cachedBook.bookTimestamp ?? cachedBook.receivedTimestamp;
      if (checkpoint === "close" && eventStart && stamp >= eventStart) {
        patch[statusColumn] = "unavailable";
        patch[observationColumn] = cachedBook.receivedTimestamp.toISOString();
        metadata[`${checkpoint}_reason`] = "POST_START_BOOK";
        pendingStatuses.push(statusColumn);
        rowUnavailable += 1;
        touched = true;
        continue;
      }

      const platform = String(row.platform ?? "").toLowerCase();
      const validPrice = cachedBook.price !== null &&
        cachedBook.price > 0 && cachedBook.price < 1;
      const validTimestamp = platform !== "polymarket" || cachedBook.bookTimestamp !== null;
      if (validPrice && validTimestamp) {
        patch[statusColumn] = "observed";
        patch[priceColumn] = cachedBook.price;
        patch[observationColumn] = cachedBook.receivedTimestamp.toISOString();
        if (cachedBook.bookTimestamp) {
          patch[bookColumn] = cachedBook.bookTimestamp.toISOString();
        }
        metadata[`${checkpoint}_reason`] = "OBSERVED";
        metadata[`${checkpoint}_obs_delay_seconds`] =
          (cachedBook.receivedTimestamp.getTime() - due.getTime()) / 1000;
        metadata[`${checkpoint}_receipt_ts`] =
          cachedBook.receivedTimestamp.toISOString();
        metadata[`${checkpoint}_book_ts_source`] = cachedBook.bookTimestamp
          ? "orderbook_timestamp"
          : "received_timestamp";
        pendingStatuses.push(statusColumn);
        rowUpdated += 1;
        touched = true;
      } else if (checkpoint === "close" && platform === "polymarket" && !validTimestamp) {
        patch[statusColumn] = "unavailable";
        patch[observationColumn] = cachedBook.receivedTimestamp.toISOString();
        metadata[`${checkpoint}_reason`] = "MISSING_ORDERBOOK_TIMESTAMP";
        pendingStatuses.push(statusColumn);
        rowUnavailable += 1;
        touched = true;
      }
    }

    if (touched) {
      patch.metadata = metadata;
      try {
        const applied = await updateRow(
          baseUrl,
          serviceKey,
          row,
          patch,
          pendingStatuses,
        );
        if (applied) {
          summary.updated += rowUpdated;
          summary.unavailable += rowUnavailable;
        } else {
          summary.conflicts += 1;
        }
      } catch (error) {
        summary.errors += 1;
        console.error(`CLV update failed ${row.candidate_id}`, error);
      }
    }
  }

  return new Response(JSON.stringify(summary), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
