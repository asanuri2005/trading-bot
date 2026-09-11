import test from "node:test";
import assert from "node:assert/strict";
import { byReturn, percentagePoints } from "./metrics.js";

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
