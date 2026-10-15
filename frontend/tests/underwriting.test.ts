// Run: node --test tests/underwriting.test.ts   (Node >= 22.6 strips the types)
import test from "node:test";
import assert from "node:assert/strict";
import {
  buyBlockers,
  canFinalize,
  conservativeTier,
  coverageLock,
  eligibility,
  hostOf,
  operatorDomain,
  revisionState,
  sourcesIndependent,
  straddledThreshold,
  tierForDeficit,
  validatorAccepts,
  WEATHER_SOURCES,
} from "../src/lib/underwriting.ts";

const FARMER = "0xBE43375329eb4A3Cafc344f0e61859905aba2B36";
const ATTO = 10n ** 18n;

test("conservative tier is the tier of (deficit - 5), floored at 0", () => {
  assert.equal(conservativeTier(50), 1);
  assert.equal(conservativeTier(55), 2);
  assert.equal(conservativeTier(54), 1);
  assert.equal(conservativeTier(29), 0);
  assert.equal(conservativeTier(30), 1);
  assert.equal(conservativeTier(3), 0);
  for (let d = 0; d <= 100; d++) assert.ok(conservativeTier(d) <= tierForDeficit(d));
});

test("validators straddling a threshold converge on the lower tier", () => {
  const hi = { tier: 2, deficit: 50, sources: 3 };
  const lo = { tier: 1, deficit: 49, sources: 3 };
  assert.equal(validatorAccepts(lo, hi), true); // A at 50% accepts leader B at 49%
  assert.equal(validatorAccepts(hi, lo), false); // B at 49% refuses a tier above its own
  assert.equal(validatorAccepts({ ...lo, deficit: 40 }, hi), false); // outside the noise band
  assert.equal(validatorAccepts({ ...lo, sources: 2 }, hi), false); // source count is exact
  assert.equal(validatorAccepts({ tier: -2, deficit: 0, sources: 1 }, { tier: 1, deficit: 30, sources: 1 }), false);
});

test("straddle detection", () => {
  assert.equal(straddledThreshold(50), 50);
  assert.equal(straddledThreshold(46), 50);
  assert.equal(straddledThreshold(44), null);
  assert.equal(straddledThreshold(28), 25);
  assert.equal(straddledThreshold(64), null);
});

test("eligibility is case-insensitive and unknown without data", () => {
  const plot = { eligible_policyholder: FARMER.toLowerCase() };
  assert.equal(eligibility(plot, FARMER), "eligible");
  assert.equal(eligibility(plot, "0x" + "ab".repeat(20)), "ineligible");
  assert.equal(eligibility(plot, null), "unknown");
  assert.equal(eligibility({}, FARMER), "unknown");
});

test("buy blockers mirror the contract gates", () => {
  const now = 1_800_000_000;
  const open = { active: true, provenance_verified: true, eligible_policyholder: FARMER, max_plot_coverage: String(5n * ATTO), active_coverage: "0", last_coverage_end: 0 };
  assert.deepEqual(buyBlockers(open, FARMER, now, ATTO), []);
  assert.deepEqual(buyBlockers({ ...open, provenance_verified: false }, FARMER, now, ATTO), ["ERR_PROVENANCE_UNVERIFIED"]);
  assert.deepEqual(buyBlockers(open, "0x" + "cd".repeat(20), now, ATTO), ["ERR_INELIGIBLE_POLICYHOLDER"]);
  assert.deepEqual(buyBlockers({ ...open, last_coverage_end: now + 10 }, FARMER, now, ATTO), ["ERR_DUPLICATE_PLOT_COVERAGE"]);
  assert.deepEqual(buyBlockers({ ...open, active_coverage: String(4n * ATTO) }, FARMER, now, 2n * ATTO), ["ERR_DUPLICATE_PLOT_COVERAGE"]);
  assert.deepEqual(buyBlockers({ ...open, active_coverage: String(4n * ATTO) }, FARMER, now, ATTO), []);
  assert.equal(coverageLock({ last_coverage_end: now }, now).locked, true); // inclusive, like the contract
  assert.equal(coverageLock({ last_coverage_end: now - 1 }, now).locked, false);
});

test("weather sources: three hostnames on three operators", () => {
  assert.equal(WEATHER_SOURCES.length, 3);
  assert.equal(new Set(WEATHER_SOURCES.map((s) => s.host)).size, 3);
  assert.equal(new Set(WEATHER_SOURCES.map((s) => operatorDomain(s.host))).size, 3);
  const urls = WEATHER_SOURCES.map((s) => `https://${s.host}/x?y=1`);
  assert.equal(sourcesIndependent(urls), true);
  // the previous design: two hostnames, one operator
  assert.equal(sourcesIndependent([urls[0], "https://api.open-meteo.com/v1/forecast", urls[1]]), false);
  assert.equal(sourcesIndependent([urls[0], urls[1], urls[1]]), false);
  assert.equal(sourcesIndependent([urls[0], urls[1], "http://www.ncei.noaa.gov/x"]), false);
  assert.equal(hostOf("https://user@Power.LARC.nasa.gov:443/a?b#c"), "power.larc.nasa.gov");
});

test("revision window", () => {
  const claim = { approved_payout: String(3n * ATTO), cooldown_until: 100_000, evaluated_at: 100, revisions: 0 };
  assert.equal(revisionState(claim, 200), "wait");
  assert.equal(revisionState(claim, 100 + 3600), "open");
  assert.equal(revisionState({ ...claim, revisions: 3, evaluated_at: 0 }, 5000), "limit");
  assert.equal(revisionState(claim, 100_000), "closed");
  assert.equal(revisionState({ ...claim, approved_payout: "0" }, 500), "none");
  assert.equal(canFinalize(claim, 99_999), false);
  assert.equal(canFinalize(claim, 100_000), true);
  assert.equal(canFinalize({ ...claim, finalized: true }, 100_000), false);
});

test("parity with the contract's Python (committed vectors)", async () => {
  const { readFileSync } = await import("node:fs");
  const v = JSON.parse(readFileSync(new URL("./parity_vectors.json", import.meta.url), "utf8"));
  for (let d = 0; d <= 100; d++) {
    assert.equal(tierForDeficit(d), v.tier[d], `tier ${d}`);
    assert.equal(conservativeTier(d), v.conservative[d], `conservative ${d}`);
  }
  for (let lead = 0; lead <= 100; lead++) {
    for (let mine = 0; mine <= 100; mine++) {
      const got = validatorAccepts(
        { tier: tierForDeficit(lead), deficit: lead, sources: 3 },
        { tier: tierForDeficit(mine), deficit: mine, sources: 3 },
      );
      assert.equal(got ? "1" : "0", v.agree[lead][mine], `agree leader=${lead} mine=${mine}`);
    }
  }
  for (const [k, urls] of Object.entries(v.urls as Record<string, string[]>)) {
    assert.equal(sourcesIndependent(urls), v.independent[k], k);
  }
});
