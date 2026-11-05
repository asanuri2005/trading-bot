import test from "node:test";
import assert from "node:assert/strict";
import { byReturn, identicalBooks, leaderGap, percentagePoints } from "./metrics.js";

const book = (name, cum_ret, positions, rets = [0, cum_ret]) => ({
  name, cum_ret, positions,
  series: rets.map((r, i) => ({ date: `2026-09-1${i}`, cum_ret: r })),
});

test("ranking uses total portfolio return rather than dollars", () => {
  const books = [{ name: "large", cum_ret: .01, cum_pnl: 1000 }, { name: "small", cum_ret: .02, cum_pnl: 200 }];
  assert.equal(byReturn(books)[0].name, "small");
  assert.equal(books[0].name, "large");
});
test("ties are stable and unavailable returns rank last", () => {
  assert.deepEqual(byReturn([{ name: "z", cum_ret: 0 }, { name: "a", cum_ret: -1e-16 }, { name: "missing" }]).map(b => b.name), ["a", "z", "missing"]);
});
test("difference between returns is percentage points", () => {
  assert.equal(percentagePoints(.05 - .02), "3.00 pp");
});

test("books with matching holdings and history are flagged as twins", () => {
  const books = [
    book("dualmom", .0646, { MU: 2.1, PSX: 7.5 }),
    book("xsmom", .0646, { MU: 2.1, PSX: 7.5 }),
    book("tsmom", -.0094, { MU: 2.1 }),
  ];
  assert.deepEqual(identicalBooks(books), { dualmom: ["xsmom"], xsmom: ["dualmom"] });
});

test("same holdings today but a different past is not a twin", () => {
  const books = [book("a", .02, { MU: 1 }, [0, .01, .02]), book("b", .02, { MU: 1 }, [0, .03, .02])];
  assert.deepEqual(identicalBooks(books), {});
});

test("the lead is measured against the best book outside the leading twins", () => {
  const books = [
    book("dualmom", .0646, { MU: 2 }), book("xsmom", .0646, { MU: 2 }), book("rsi2", .012, {}),
  ];
  const { leaders, gap } = leaderGap(books);
  assert.deepEqual(leaders.map((b) => b.name), ["dualmom", "xsmom"]);
  assert.equal(percentagePoints(gap), "5.26 pp");
  assert.equal(leaderGap([books[0], books[1]]).gap, null);
});
