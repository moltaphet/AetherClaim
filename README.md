# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| Contract | `0x2641A4678ba75EE21ad6f8A7436209B5675f8B23` |
| Explorer | https://explorer-studio-next.genlayer.com/address/0x2641A4678ba75EE21ad6f8A7436209B5675f8B23 |

> **Deployment status (honest note).** This is the **round-4 hardened** build
> (arid-term refusal at purchase, Central Valley winter calendar, 30-day outage
> grace, disagreement-final settlement). It is deployed to Studio Next with the
> 0.5 GEN liquidity seed accepted (`deployments/studio-next.json`). The served
> schema matches `deployments/aether_claim_abi.json` (19 methods, identical name
> set). Studio's `gen_call` read endpoint has been failing server-side (`Can't
> reconnect until invalid transaction is rolled back`) for every contract, so
> the on-chain genesis registry and pool state have **not been read back**. The
> frontend falls back to cached genesis data until reads recover.

## 1. Architecture

```
            ┌────────────────────── AetherClaim contract ──────────────────────┐
 LP ──GEN──▶│ provide_liquidity        pool_balance / total_liabilities        │
 Farmer ───▶│ create_policy(plot,cov)  plots{} (admin registry, 7-day staging) │
            │   refuses arid + off-season terms   policies{}  epoch_exposure{} │
 Anyone ───▶│ evaluate_claim ──────┐   claims{}                                │
            │                      ▼                                          │
            │   leader: 3 sources → per-source tier (min of 2 windows)         │
            │           → corroborate → {tier, median_deficit, n, reasoning}   │
            │   validators: re-run, require EXACT tier (or class) equality     │
 Anyone ───▶│ release_payout   24h cooldown → 3-day linear vesting → holder    │
            └──────────────────────────────────────────────────────────────────┘
   Open-Meteo ERA5 archive ── Open-Meteo GFS (NOAA model) ── NASA POWER
```

| Deficit vs. baseline | Tier | Payout |
|---|---|---|
| < 25% | 0 NORMAL | 0% |
| 25–49% | 1 MODERATE_DROUGHT | 30% |
| 50–74% | 2 SEVERE_DROUGHT | 70% |
| ≥ 75% | 3 CATASTROPHIC_DROUGHT | 100% |

### 1a. How a policy is judged

- **Windows.** A policy is judged on **two consecutive 14-day windows that are the
  final 28 days of its term**. `observation_end` is the last full day inside the
  term. `observation_start` is 27 days before it. Both are written at purchase and
  never move.
- **Maturity.** The verdict may be taken only from `matures_at` = term end + 6
  days, the reanalysis lag. Data for the final day is published by then.
- **Baselines.** Each window is judged against the baseline of **its own calendar
  month**, taken at the window midpoint: `window baseline = monthly_mm × 14 / 30`.
- **Micro-baseline floor.** A window whose baseline is under **10 mm** cannot
  evidence a drought and reads 0%. The purchase check uses the same window math,
  so a term with any arid window is refused outright (see §2).

### 1b. Insurable calendar: Central Valley (Almond / winter crop)

The genesis plot `CV-ALMOND-01` has a crop cycle of **November through March**
(months 11, 12, 1, 2, 3). Its monthly rainfall baselines (mm, Jan→Dec) are:

| Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 45 | 35 | 15 | 8 | 2 | 0 | 0 | 4 | 15 | 25 | 45 |

Those values are the Mediterranean winter pattern supplied for this round. They
are **illustrative and not checked against station normals**. Validate them
before real money is involved.

**Purchase window.** A 90-day term must be entirely inside the cycle, so the
insurable purchase dates are **1 November through 31 December** each year:

- Starting **1 Nov** gives a term ending **30 Jan**, with windows in January.
- Starting **31 Dec** gives a term ending **31 Mar**, with windows in March.
- Starting **1 Jan** reaches April, which is off-season, and the purchase reverts.

All cycle windows clear the 10 mm floor (Nov 11.7 mm, Dec 21.0, Jan 23.3, Feb
21.0, Mar 16.3 per window). The calendar does the work here, not the floor. The
floor exists for other plots and protects against a future change to the data.

The frontend shows the next insurable start date for the selected plot.

## 2. Game-theoretic security & anti-exploit design

**Economic non-determinism (TruScore flaw).** Validators never compare
continuous values within a tolerance. Each validator recomputes the integer
tier and the validator function requires `leader.tier == validator.tier`
exactly. The payout is `coverage × {0, 3000, 7000, 10000} / 10000`, so any two
validators that accept move identical money. Borderline cases (49% vs 50%
deficit) are *rejected* rather than averaged — see
`test_economic_drift_rejected_by_validator`. The reasoning string is
deterministic code output, not LLM prose.

**Self-attack / moral hazard (PulseSLA flaw).**
- *No arbitrary coordinates.* `create_policy` accepts only a `plot_id` present
  and active in the registry. Latitude, longitude, baselines and cadastral hash
  are written by the admin after a 7-day staging delay. Observation URLs are
  built *inside the contract* from registry values, so nothing a policyholder
  passes ever reaches a fetch.
- *The insured cannot cause the insured event.* Rainfall is not influenced by
  the policyholder. The remaining attack surface is timing (see *Fixed-window
  settlement* below).
- *Worthless policies are refused.* `create_policy` reverts with
  `Term uninsurable: observation window falls in arid month (< 10mm baseline)`
  if **either** final window's baseline is under the floor. A policy that could
  only settle at 0% is never sold (`test_arid_term_reverts_at_purchase`, with an
  arid March and an arid April each refused on its own).
- *Single-source / isolated fault.* At least 2 of 3 sources must answer with
  complete data. If the answering sources **disagree** on tier, the result is
  final and pays nothing (`test_single_source_fault_is_final_and_cannot_be_waited_out`).
- *One-off anomaly.* The tier is the minimum over the two windows.

**Fixed-window settlement and what it does and does not prevent.** Because
`observation_start` and `observation_end` are recorded at purchase, no evaluation
time can move the windows. A policyholder cannot choose which weather days are
judged, which is the American-style exercise that options markets guard against.
The verdict is also taken once: a settled outcome is final, and a disagreement
between sources is final too. A retry therefore cannot wait for a dissenting
source to go down, which closes the retry-based version of the attack.

It does **not** remove the timing choice of *when a call is made* relative to
source availability. If the two sources that agree on a breach are up while the
dissenting source is down, a call at that moment settles on two sources. See the
first item under *Known limits*.

**Solvency & timing.**
- Per-epoch (7d) coverage cap `max_epoch_exposure`, single-policy
  concentration cap (25% of pool), and `total_liabilities ≤ pool_balance`.
- LPs can only withdraw capital not reserved against live policies.
- Approved payouts wait 24h, then vest linearly over 3 days via
  `release_payout` (effects before transfer).
- **Outage tolerance.** A quorum failure (fewer than 2 sources answer) is an
  outage, not a verdict. The policy stays open. A retry is allowed once a day
  until **30 days after term end**. Expiry then waits **14 days after the last
  attempt**, so a retry window is never closed under the policyholder's feet
  (`test_outage_retry_window_survives_until_day_thirty`).
- **Final outcomes.** A Tier 0 result, an approved claim, or a disagreement all
  close the policy. A Tier 0 or disagreement settlement releases the reserve at
  once.
- **Orphan guard.** The last LP cannot exit while policies are live
  (`test_last_lp_cannot_orphan_reserved_capital`). Burning every share would
  strand the reserve with no shareholder, and the next depositor would inherit
  it. The accounting guard in `provide_liquidity` fails closed on any state where
  capital and shares disagree. Those states are unreachable through the public
  API, because any surviving share keeps at least 1 wei of free capital.

**Known limits.** Read these before relying on the product.

1. **Source-availability timing (open).** The 2-of-3 quorum settles on whichever
   two sources answer. A policyholder who watches the public endpoints can call
   `evaluate_claim` during an outage of the dissenting source and settle on the
   other two. Fixed windows do not prevent this, and the 30-day grace extends the
   time in which it could happen. Options that close it, none implemented here:
   require unanimity (3 of 3) for payouts, so an outage can only produce a retry;
   let only a keeper call `evaluate_claim` at maturity; or commit to availability
   on-chain before the verdict. Requiring unanimity is the strongest and the
   simplest. It is a product decision, because it changes the quorum rule the
   spec defines.
2. **Two of three sources share one provider.** ERA5 (`archive-api.open-meteo.com`)
   and GFS (`api.open-meteo.com`) are both served by Open-Meteo. A single
   provider outage removes two of the three sources, leaving NASA POWER alone,
   which cannot reach quorum. A compromise of that provider could supply two
   agreeing false readings and pass the 2-of-3 rule. Production should diversify
   the GFS leg to a direct NOAA source (NOMADS/NWS) on an independent host, and
   add an independent archive such as Copernicus/CDS, with NASA POWER kept.
3. **Disagreement is final.** The same rule that closes the retry lever means a
   transient bad reading from one source at one moment settles the policy for
   good. A policyholder who is hurt by a glitch cannot retry it away. That is the
   price of closing the retry lever. The trade-off is deliberate, and you can
   change it.
4. **Admin ban binds one address, not a person.** An admin can still use a second
   wallet. Use a multisig for the admin, and watch registry changes (plots are
   public for 7 days before activation).
5. **Capital is locked longer.** Expired-but-unevaluated policies keep their
   reserve until day 30, and up to 14 days after the last outage retry. LP
   capital is locked for that period.
6. **Weather sources are public APIs, not a hardware oracle.** NASA POWER is a
   reanalysis product with a publication lag, and the premium is a flat 8% with
   no actuarial pricing.
7. **Not audited.** This is hackathon-grade code.

## 2b. Audit remediation

| # | Finding | Fix |
|---|---|---|
| 1 | One scalar baseline made every dry season look like a drought | **Monthly actuarial baselines** (12 values) + validated crop cycle. `create_policy` rejects any term that touches an off-season month; each window is compared with its own month's baseline. Regression: `test_seasonal_summer_exploit_prevented`, `test_per_month_baselines_drive_the_deficit` |
| 2 | Admin could stage a plot, insure it, and self-pay | `create_policy` reverts for the admin address; plot activation timelock is **7 days**; `set_max_epoch_exposure` is bounded. Regression: `test_admin_self_insurance_prevented` |
| 3/4 | Flat principal accounting: premiums unowned, losses fall on the last LP out | **ERC-4626-style share vault**: shares priced against `pool_balance` before the deposit lands; `withdraw_liquidity(shares)` pays `shares × pool_balance / total_shares`. Premiums raise share price, paid claims lower it for all LPs equally. Regression: `test_lp_earns_accumulated_premiums`, `test_lp_share_fair_loss_distribution` |
| 5 | Validators only compared `tier`; leader-supplied fields were stored unchecked | Validators reject any payload that fails strict structure and bounds checks (`tier ∈ [-2,3]`, `0 ≤ median_deficit ≤ 100`, ints not bools, quorum rules per class, deficit inside the tier bucket, printable ≤ 400-char reasoning). The contract re-checks before writing and **stores a reasoning string it builds itself**. Regression: `test_validator_rejects_malformed_leader_payload` |
| 6 | Last LP burns 100% of shares while the reserve is live: capital is orphaned | `withdraw_liquidity` reverts when `total_lp_shares − burn == 0` and `total_liabilities > 0`. `provide_liquidity` fails closed on capital-without-shares or shares-without-capital. Regression: `test_last_lp_cannot_orphan_reserved_capital` |
| 7 | Arid windows (0.9 mm expected) read 100% on trivial shortfalls | Windows with baseline under `MIN_INSURABLE_BASELINE_MM = 10` read 0% at settlement |
| 8 | Caller could evaluate on any day, choosing the best window | European exercise: windows fixed at purchase to the term's final 28 days, verdict only at `matures_at`, one final settlement. Regression: `test_claim_cannot_be_evaluated_before_observation_maturity`, `test_observation_window_is_fixed_to_final_28_days` |
| 9 (round 4) | Arid terms could be sold with a guaranteed 0% payout | `create_policy` refuses any term whose final window is under the floor, using the settlement's window math. A monthly check would still sell 10–21 mm months whose windows are under the floor, so the check is per window. Regression: `test_arid_term_reverts_at_purchase`, `test_arid_control_plot_still_insurable` |
| 10 (round 4) | Central Valley had Apr–Oct as its cycle, which is dormant and dry | Cycle set to **Nov–Mar** with Mediterranean winter baselines. Regression: `test_central_valley_winter_policy_insurable_and_settles`, `test_central_valley_summer_term_rejected`, `test_central_valley_purchase_calendar` |
| 11 (round 4) | A 14-day grace let third-party outages forfeit a valid claim | Grace raised to **30 days**. A quorum failure stays retryable. Expiry waits 14 days after the last attempt. Regression: `test_outage_retry_window_survives_until_day_thirty`, `test_evaluation_closes_after_grace`, `test_expire_releases_reserve_after_grace` |
| 12 (round 4) | A retry after a source split could wait for the dissenter to go down | A quorum failure (`INCONCLUSIVE_QUORUM`) is retryable. A source disagreement (`INCONCLUSIVE_DISAGREE`) is final. The class is a consensus value compared exactly, so a leader cannot relabel one as the other. Regression: `test_single_source_fault_is_final_and_cannot_be_waited_out`, `test_outcome_class_is_consensus_checked` |

**Deliberate deviation from the plain ERC-4626 formula.** A withdrawal pays
`min(shares × pool / total_shares, shares × (pool − reserved) / total_shares)`.
With only the first term, an LP could exit while a claim is still unrecognised
and leave the remaining LPs holding the loss (the "early exit" exploit). The cap
means reserved capital never leaves ahead of the claims it backs.
`test_lp_share_fair_loss_distribution` proves the leaver and the stayer end with
identical value. The price is that a full exit while policies are live returns
slightly less than the headline share price until they resolve.

## 3. Verify

```bash
# contract tests (56 direct-mode tests: lifecycle, adversarial, audit regressions)
python -m pytest tests/test_aether_claim.py -v   # needs genlayer-test (gltest); pins GENVM_VERSION in tests/conftest.py

# deploy (CLI already configured with a funded account)
genlayer network set studio-next
genlayer deploy              # runs deploy/deploy_aether_claim.ts

# read checks (currently failing on Studio; see the deployment note)
genlayer call 0x2641A4678ba75EE21ad6f8A7436209B5675f8B23 list_plots
genlayer call 0x2641A4678ba75EE21ad6f8A7436209B5675f8B23 get_pool
genlayer call 0x2641A4678ba75EE21ad6f8A7436209B5675f8B23 get_config

# frontend
cd frontend && npm install && npm run dev
```

## 4. Layout

```
contracts/aether_claim.py      Intelligent contract
tests/                         pytest direct-mode suite (+ validator harness)
deploy/deploy_aether_claim.ts  deploy + fee estimation + liquidity seed + ABI export
deployments/                   ABI schema + deployment record
frontend/                      Next.js 14 + Tailwind dapp
```

Frontend notes: writes go through the injected EIP-1193 provider into the
official `genlayer-js` client. Before each write the fee deposit comes from
`estimateTransactionFeesForWrite` (falling back to `estimateTransactionFees`,
never a zero deposit) and the gas limit from `estimateTransactionGas`; nothing
is hardcoded. MetaMask may still show its own EVM simulation warning on Studio
Next, which is independent of GenLayer protocol fees. Reads race the two RPCs
under a 5s limit and fall back to the genesis registry. Wallet-side behaviour
was not exercised against a live MetaMask in this build.
