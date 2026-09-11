import React, { useEffect, useMemo, useState } from "react";
import { byReturn, percentagePoints } from "./metrics";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

// Six categorical hues stepped for this dark surface (#161f2b) and validated
// as a set: lightness band, chroma floor, colourblind separation, normal-vision
// separation, and >=3:1 contrast all pass. Order is fixed — never cycle or
// regenerate a hue for a seventh book; give it a slot here instead.
const SERIES_COLORS = [
  "#3987e5", // blue
  "#d95926", // orange
  "#199e70", // aqua
  "#c98500", // yellow
  "#d55181", // magenta
  "#008300", // green
  "#d9e0ea", // dashed buy-and-hold reference
];

// Colour follows the book, never its position in the list: the reporter sorts
// strategies alphabetically, so a book added mid-alphabet would otherwise
// repaint every book after it and silently invalidate every past screenshot.
const BOOK_SLOT = {
  tsmom_126: 0,
  rsi2_lt10_r200: 1,
  ml_logreg: 2,
  xsmom_126_top3: 3,
  dualmom_126_top3: 4,
  ens_2of3: 5,
  buy_and_hold: 6,
};

// An unregistered book still gets a stable colour rather than a shifting one.
// Two could collide; the fix is to give it a slot above, not to cycle by index.
function colorFor(name) {
  const slot = BOOK_SLOT[name];
  if (slot !== undefined) return SERIES_COLORS[slot];
  let h = 0;
  for (let i = 0; i < String(name).length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return SERIES_COLORS[Math.abs(h) % SERIES_COLORS.length];
}

const usd = (v, signed = false) => {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  const sign = v > 0 && signed ? "+" : "";
  return (
    sign +
    Number(v).toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2,
    })
  );
};

// Takes a fraction (0.0123), renders a percentage ("+1.23%"). Anything that
// rounds to zero is snapped to exactly zero first: rebasing divides compounded
// returns, which leaves float residue that would otherwise render "-0.00%".
const pct = (v, signed = false) => {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  let n = Number(v || 0) * 100;
  if (Math.abs(n) < 0.005) n = 0;
  return `${n > 0 && signed ? "+" : ""}${n.toFixed(2)}%`;
};

function useData() {
  const [state, setState] = useState({ loading: true, data: null, sample: false });
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const sample = new URLSearchParams(window.location.search).get("sample") === "1";
    const refresh = async () => {
      try {
        const res = await fetch(sample ? "sample-data.json" : "data.json", { cache: "no-store", signal: controller.signal });
        if (!res.ok) throw new Error("Data unavailable");
        const data = await res.json();
        if (!Array.isArray(data.strategies)) throw new Error("Invalid ledger");
        if (!cancelled) setState({ loading: false, data, sample, error: false });
      } catch {
        if (!cancelled) setState(s => ({ ...s, loading: false, error: true }));
      }
    };
    refresh();
    const timer = setInterval(refresh, 60000);
    return () => { cancelled = true; controller.abort(); clearInterval(timer); };
  }, []);
  return state;
}

function PnlDelta({ value, pctValue, label = "today" }) {
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "flat";
  const text =
    value === 0
      ? `flat ${label}`
      : `${usd(value, true)} · ${pct(pctValue, true)} ${label}`;
  return <div className={`delta ${cls}`}>{text}</div>;
}

function BookCard({ book, color }) {
  const positions = Object.entries(book.positions || {});
  return (
    <section className="card book" style={{ "--book-color": color }}>
      <h2>{book.display}</h2>
      {/* Return leads, dollars follow: the dollar figure mostly tracks how
          many names a book happens to hold, not how well it picked them. */}
      <p className={`big ${(book.cum_ret || 0) >= 0 ? "pos" : "neg"}`}>
        {pct(book.cum_ret, true)}
      </p>
      <div className="subline">
        {usd(book.cum_pnl, true)} P&amp;L · {usd(book.equity)} total equity
      </div>
      <div className="subline">{usd(book.cash)} cash · {usd(book.capital)} invested</div>
      <div className="subline">Starting budget {usd(book.initial_budget)}</div>
      <PnlDelta value={book.day_pnl} pctValue={book.day_ret} />
      <div className="holdings">
        {positions.length === 0
          ? "No open positions — waiting for a signal."
          : positions.map(([sym, qty]) => (
              <div key={sym}>
                simulated <span>{Number(qty).toFixed(3)} {sym} units</span>
              </div>
            ))}
      </div>
    </section>
  );
}

// Books are ranked on return, not dollars: one holding three names cannot be
// compared to one holding twelve on P&L alone, since the wider book simply has
// more capital at work. Percentage removes the size difference.

function Scoreline({ books }) {
  if (books.length < 2) return null;
  const [leader, runnerUp] = byReturn(books);
  const gap = (leader.cum_ret || 0) - (runnerUp.cum_ret || 0);
  if (Math.abs(gap) < 0.00005) return <div className="scoreline flat">dead even</div>;
  return (
    <div className="scoreline">
      <span style={{ color: colorFor(leader.name) }}>{leader.display}</span>{" "}
      leads by {percentagePoints(gap)}
    </div>
  );
}

function RaceChart({ books, metric }) {
  const key = metric === "usd" ? "cum_pnl" : "cum_ret";
  const fmt = metric === "usd" ? usd : pct;
  const rows = useMemo(() => {
    const byDate = new Map();
    books.forEach((book) => {
      (book.series || []).forEach((point) => {
        if (!byDate.has(point.date)) byDate.set(point.date, { date: point.date });
        byDate.get(point.date)[book.name] = point[key] ?? null;
      });
    });
    return [...byDate.values()].sort((x, y) => x.date.localeCompare(y.date));
  }, [books, key]);

  if (rows.length < 2) {
    return (
      <div className="empty">
        <strong>Not enough history to chart yet</strong>
        The reporter adds one point per market day. Check back after the next market close.
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
        <CartesianGrid stroke="#222d3c" strokeDasharray="3 6" vertical={false} />
        <XAxis
          dataKey="date"
          tick={{ fill: "#8b98a9", fontSize: 11, fontFamily: "IBM Plex Mono" }}
          tickLine={false}
          axisLine={{ stroke: "#263344" }}
          minTickGap={48}
        />
        <YAxis
          tickFormatter={(v) => fmt(v)}
          tick={{ fill: "#8b98a9", fontSize: 11, fontFamily: "IBM Plex Mono" }}
          tickLine={false}
          axisLine={false}
          width={72}
        />
        <Tooltip
          contentStyle={{
            background: "#121a25",
            border: "1px solid #263344",
            borderRadius: 6,
            fontFamily: "IBM Plex Mono",
            fontSize: 12,
          }}
          labelStyle={{ color: "#8b98a9" }}
          formatter={(value, name) => [
            fmt(value, true),
            books.find((b) => b.name === name)?.display || name,
          ]}
        />
        {books.map((book, i) => (
          <Line
            key={book.name}
            type="monotone"
            dataKey={book.name}
            stroke={colorFor(book.name)}
            strokeWidth={2}
            strokeDasharray={book.name === "buy_and_hold" ? "7 4" : undefined}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

function TargetsTable({ targets }) {
  return (
    <section className="card">
      <table>
        <caption>Targets today</caption>
        <thead>
          <tr>
            <th>Book</th>
            <th>Symbol</th>
            <th>Stance</th>
            <th className="num">Target weight</th>
          </tr>
        </thead>
        <tbody>
          {targets.map((t, i) => (
            <tr key={i}>
              <td>
                <span
                  className="dot"
                  style={{ background: colorFor(t.strategy) }}
                />
                {t.strategy}
              </td>
              <td>{t.symbol}</td>
              <td>
                {t.signal === 1 ? (
                  <span className="tag long">long</span>
                ) : (
                  <span className="tag flatpos">flat</span>
                )}
              </td>
              <td className="num">{pct(t.weight)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function TradesTable({ trades }) {
  return (
    <section className="card">
      <table>
        <caption>Broker orders (separate from simulated books)</caption>
        <thead>
          <tr>
            <th>Date</th>
            <th>Side</th>
            <th>Symbol</th>
            <th className="num">Filled / ordered</th>
            <th className="num">Avg fill</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {trades.length === 0 && (
            <tr>
              <td colSpan={6} className="flat">
                No broker orders recorded. In dry-run mode, intended orders appear in
                the executor's CloudWatch logs.
              </td>
            </tr>
          )}
          {trades.slice(0, 12).map((t, i) => (
            <tr key={i}>
              <td>{String(t.ts).slice(0, 10)}</td>
              <td>
                <span className={`tag ${t.side}`}>{t.side}</span>
              </td>
              <td>{t.symbol}</td>
              <td className="num">{t.filled_qty ?? "—"} / {t.qty}</td>
              <td className="num">{t.price ? usd(t.price) : "—"}</td>
              <td>{t.status || "unknown"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// A strategy added to the lineup later starts at zero while its rivals keep
// P&L they earned before it existed, so the raw cum_pnl ranking is unfair to
// the newcomer. Measure every book from the latest date they all share:
// subtract each book's cum_pnl at that date and drop earlier points. Nothing
// is mutated in DynamoDB — the stored record keeps each book's true history
// since its own inception, and this only changes how the race is displayed.
function rebaseToCommonStart(raw) {
  const withSeries = raw.filter((b) => (b.series || []).length);
  if (withSeries.length < 2) return { books: raw, start: null, rebased: false };

  const start = withSeries.reduce(
    (latest, b) => (b.series[0].date > latest ? b.series[0].date : latest),
    withSeries[0].series[0].date,
  );

  // Dollars rebase by subtraction. Returns compound, so they rebase by
  // division — (1+r)/(1+r_start) - 1 — never by subtracting one percentage
  // from another, which would quietly overstate the gap on long runs.
  const books = raw.map((book) => {
    const series = book.series || [];
    const upToStart = series.filter((p) => p.date <= start);
    if (!upToStart.length) return book;
    const anchor = upToStart[upToStart.length - 1];
    const basePnl = anchor.cum_pnl || 0;
    const baseRet = anchor.cum_ret || 0;
    const reret = (r) => (1 + (r || 0)) / (1 + baseRet) - 1;
    return {
      ...book,
      cum_pnl: book.cum_pnl - basePnl,
      cum_ret: reret(book.cum_ret),
      series: series
        .filter((p) => p.date >= start)
        .map((p) => ({
          ...p,
          cum_pnl: p.cum_pnl - basePnl,
          cum_ret: reret(p.cum_ret),
        })),
    };
  });

  return {
    books,
    start,
    rebased: books.some((b, i) => b.cum_pnl !== raw[i].cum_pnl),
  };
}

export default function App() {
  const { loading, data, sample, error } = useData();

  const [metric, setMetric] = useState("ret");

  const { books: levelled, start: commonStart, rebased } = useMemo(
    () => data?.schema_version === 2
      ? { books: data.strategies, start: data.start_date, rebased: false }
      : rebaseToCommonStart(data?.strategies || []),
    [data],
  );
  // Leaderboard order — best return first. Colours are keyed by book name, so
  // reordering here never repaints a line.
  const books = useMemo(() => byReturn(levelled), [levelled]);
  return (
    <div className="wrap">
      <header className="masthead">
        <div>
          <div className="eyebrow">Serverless trading bot</div>
          <h1 className="wordmark">
            Paper Ledger
            {sample && <span className="badge sample">sample data</span>}
          </h1>
        </div>
        {data && (
          <div className="meta">
            Alpaca account equity <strong>{usd(data.account?.equity)}</strong>
            <br />
            as of {data.as_of} · published{" "}
            {String(data.generated_at).slice(11, 16)}Z
          </div>
        )}
      </header>

      {loading && <div className="empty">Loading the ledger…</div>}
      {error && <div className="card empty" role="status">Refresh failed. Any displayed figures are the last successful snapshot.</div>}

      {!loading && !data && (
        <div className="card empty">
          <strong>No data published yet</strong>
          The reporter Lambda writes data.json after the first market close.
          Invoke it manually to publish now:&nbsp;
          <code>aws lambda invoke --function-name &lt;ReporterFunctionName&gt; /dev/stdout</code>
        </div>
      )}

      {!loading && data && (
        <>
          <div className="card comparison-note">
            {data.schema_version === 2 ? <>
              <strong>Simulated comparison · {usd(data.book_budget)} starting budget per book</strong>
              <p>Returns include idle cash and {data.cost_bps} bps per side in modeled trading costs.
              Orders and fills in Alpaca are separate. Buy &amp; hold is the dashed reference line.</p>
              <div className="subline">Started {data.start_date} · refreshes every minute</div>
            </> : <strong>Legacy comparison: percentages exclude idle cash. Awaiting the new experiment.</strong>}
          </div>
          <div className="books">
            {books.map((book) => (
              <BookCard
                key={book.name}
                book={book}
                color={colorFor(book.name)}
              />
            ))}
          </div>

          <section className="card chart-card">
            <div className="chart-head">
              <div className="chart-title">
                {metric === "usd" ? "Cumulative P&L" : "Cumulative return"}
                {commonStart ? ` since ${commonStart}` : " since inception"}
              </div>
              <div className="chart-tools">
                <div className="toggle" role="group" aria-label="Chart metric">
                  <button
                    type="button"
                    className={metric === "ret" ? "on" : ""}
                    aria-pressed={metric === "ret"}
                    onClick={() => setMetric("ret")}
                  >
                    %
                  </button>
                  <button
                    type="button"
                    className={metric === "usd" ? "on" : ""}
                    aria-pressed={metric === "usd"}
                    onClick={() => setMetric("usd")}
                  >
                    $
                  </button>
                </div>
                <Scoreline books={books} />
              </div>
            </div>
            <RaceChart books={books} metric={metric} />
            {/* Six lines is past the point where a reader can trace one back
                to a card above, so identity is spelled out beside the plot. */}
            <ul className="legend">
              {books.map((book) => (
                <li key={book.name}>
                  <span
                    className="swatch"
                    style={{ background: colorFor(book.name) }}
                  />
                  {book.display}
                </li>
              ))}
            </ul>
          </section>

          <div className="grid-2">
            <TargetsTable targets={data.targets || []} />
            <TradesTable trades={data.trades || []} />
          </div>

          <footer>
            Shadow books use fractional synthetic units and adjusted closes, with modeled costs.
            They are not executed portfolios. The paper executor uses whole shares and broker fills.
          </footer>
        </>
      )}
    </div>
  );
}
