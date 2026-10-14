# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| Contract | `0x6Ee88b72fd33A3e6FB475Aae3f966e9b58fa213b` |
| Explorer | https://explorer-studio-next.genlayer.com/address/0x6Ee88b72fd33A3e6FB475Aae3f966e9b58fa213b |

> **Deployment status (honest note).** This is the **round-3 hardened** build
> (last-LP orphan guard, micro-baseline floor, European exercise). It is deployed
> to Studio Next with the 0.5 GEN liquidity seed accepted
> (`deployments/studio-next.json`). The served schema matches
> `deployments/aether_claim_abi.json` (19 methods, identical name set). Studio's
> `gen_call` read endpoint has been failing server-side (`Can't reconnect until
> invalid transaction is rolled back`) for every contract, so the on-chain
> genesis registry and pool state have **not been read back**. The frontend
> falls back to cached genesis data until reads recover. Run the read checks in
> section 3 to confirm.

## 1. Architecture

```
            ┌────────────────────── AetherClaim contract ──────────────────────┐
 LP ──GEN──▶│ provide_liquidity        pool_balance / total_liabilities        │
 Farmer ───▶│ create_policy(plot,cov)  plots{} (admin registry, 24h staging)   │
            │   premium 8% exact       policies{}   epoch_exposure{}           │
 Anyone ───▶│ evaluate_claim ──────┐   claims{}                                │
            │                      ▼                                          │
            │   leader: 3 sources → per-source tier (min of 2 windows)         │
            │           → corroborate → {tier, median_deficit, n, reasoning}   │
            │   validators: re-run, require EXACT tier equality                │
 Anyone ───▶│ release_payout   24h cooldown → 3-day linear vesting → holder    │
            └──────────────────────────────────────────────────────────────────┘
   Open-Meteo ERA5 archive ── NOAA GFS (via Open-Meteo) ── NASA POWER
```

| Deficit vs. baseline | Tier | Payout |
|---|---|---|
| < 25% | 0 NORMAL | 0% |
| 25–49% | 1 MODERATE_DROUGHT | 30% |
| 50–74% | 2 SEVERE_DROUGHT | 70% |
| ≥ 75% | 3 CATASTROPHIC_DROUGHT | 100% |

A policy is judged on **two consecutive 14-day windows that are the final 28 days
of its term**. The windows are fixed at purchase (`observation_start`,
`observation_end`), so the policyholder cannot choose the days they are judged
on. The verdict can be taken once, at `matures_at` = term end + 6 days (the
reanalysis lag). Each window is judged against the baseline of **its own
calendar month** (the month of the window midpoint):
`window baseline = monthly_mm × 14 / 30`. A window whose baseline is under
**10 mm** is treated as non-insurable and reads 0%. A plot carries 12 monthly
baselines and a crop cycle (`start_month`..`end_month`, may wrap the year).

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
  and active in the registry. Latitude/longitude, baseline and cadastral hash
  are written by the admin, after a 24h staging delay. Observation URLs are
  built *inside the contract* from registry values; nothing a policyholder
  passes ever reaches a fetch.
- *The insured cannot cause the insured event.* Rainfall is not influenced by
  the policyholder. The remaining attack surface is timing. Windows are the
  final 28 days of the term, fixed at purchase, and the verdict cannot be taken
  before `matures_at` (`test_claim_cannot_be_evaluated_before_observation_maturity`).
  Weather from before purchase is never measured, and the policyholder cannot
  check the weather day by day and claim on a good day.
- *Single-source / isolated fault.* ≥ 2 of 3 independent sources must return
  HTTP 200 with complete data and **all** answering sources must land in the
  same tier; otherwise the round is `INCONCLUSIVE` and pays 0.
- *One-off anomaly.* The tier is the minimum over two consecutive windows.
- *Micro-baseline floor.* A window expecting under 10 mm reads 0%. A
  2 mm/month plot with zero rain cannot reach Tier 3
  (`test_arid_micro_baseline_evaluates_to_zero_deficit`). Because the verdict is
  the minimum, one arid window in the pair caps the whole policy at Tier 0.

**Solvency & timing.**
- Per-epoch (7d) coverage cap `max_epoch_exposure`, single-policy
  concentration cap (25% of pool), and `total_liabilities ≤ pool_balance`.
- LPs can only withdraw capital not reserved against live policies.
- Approved payouts wait 24h, then vest linearly over 3 days via
  `release_payout` (effects before transfer).
- **European exercise.** A settled outcome (Tier 0 or an approved claim) is
  final, and a Tier 0 settlement releases the reserve at once. `INCONCLUSIVE`
  leaves the policy open for a retry, at most once per day, until the grace
  period ends. A retry re-reads the same fixed windows. It only helps if a
  source recovers.
- **Orphan guard.** The last LP cannot exit while policies are live
  (`test_last_lp_cannot_orphan_reserved_capital`). Burning every share would
  strand the reserve with no shareholder, and the next depositor would inherit
  it. The accounting guard in `provide_liquidity` fails closed on any state where
  capital and shares disagree. Those states are unreachable through the public
  API, because any surviving share keeps at least 1 wei of free capital.

**Known limits.** The admin ban binds one address, not a person: an admin can
still use a second wallet, so the admin should be a multisig and registry
changes should be watched (plots are public for 7 days before activation). The
2-of-3 rule means a persistent single-source outage still
settles on two sources; all sources are public weather pipelines (not a
hardware oracle); the admin is a single address (use a multisig); NOAA is
consumed via Open-Meteo's GFS model rather than NWS directly; the premium is a
flat 8% (no actuarial pricing). This is hackathon-grade, not audited.

Two product consequences of the floor, flagged for a governance decision:
- **Arid terms can still be sold.** `create_policy` does not reject a term whose
  final windows fall below the floor, so a buyer can pay a premium for a policy
  that can only settle at 0%. The test suite requires this path to exist.
  Rejecting such terms at purchase would be a one-line change, but it would
  also block the arid-plot regression test.
- **Genesis data.** The Central Valley Almond plot's April to October windows
  all fall below the floor, so in practice its insurable months are November to
  March. The baselines are illustrative, not sourced climatology.

## 2b. Audit remediation (institutional hardening)

| # | Finding | Fix |
|---|---|---|
| 1 | One scalar baseline made every dry season look like a drought | **Monthly actuarial baselines** (12 values) + validated crop cycle. `create_policy` rejects any term that touches an off-season month; each window is compared with its own month's baseline. Regression: `test_seasonal_summer_exploit_prevented`, `test_per_month_baselines_drive_the_deficit` |
| 2 | Admin could stage a plot, insure it, and self-pay | `create_policy` reverts for the admin address; plot activation timelock is now **7 days**; `set_max_epoch_exposure` is bounded. Regression: `test_admin_self_insurance_prevented` |
| 3/4 | Flat principal accounting: premiums unowned, losses fall on the last LP out | **ERC-4626-style share vault**: shares priced against `pool_balance` before the deposit lands; `withdraw_liquidity(shares)` pays `shares × pool_balance / total_shares`. Premiums raise share price, paid claims lower it for all LPs equally. Regression: `test_lp_earns_accumulated_premiums`, `test_lp_share_fair_loss_distribution` |
| 5 | Validators only compared `tier`; leader-supplied fields were stored unchecked | Validators reject any payload that fails strict structure/bounds (`tier ∈ [-1,3]`, `0 ≤ median_deficit ≤ 100`, ints not bools, quorum ≥ 2 for a settled tier, deficit inside the tier bucket, printable ≤ 400-char reasoning). The contract re-checks before writing and **stores a reasoning string it builds itself**, so no leader text reaches state. Regression: `test_validator_rejects_malformed_leader_payload` |
| 6 (round 3) | Last LP burns 100% of shares while reserve is live: capital is orphaned and the next depositor inherits it | `withdraw_liquidity` reverts when `total_lp_shares − burn == 0` and `total_liabilities > 0`. `provide_liquidity` fails closed on capital-without-shares or shares-without-capital. Regression: `test_last_lp_cannot_orphan_reserved_capital` |
| 7 (round 3) | Arid windows (0.9 mm expected) read 100% on trivial shortfalls | Windows with baseline under `MIN_INSURABLE_BASELINE_MM = 10` read 0%. Regression: `test_arid_micro_baseline_evaluates_to_zero_deficit` (with a 30 mm/month control that still pays Tier 3) |
| 8 (round 3) | Caller could evaluate on any day, choosing the best window | European exercise: windows fixed at purchase to the term's final 28 days, verdict only at `matures_at`, one final settlement. Regression: `test_claim_cannot_be_evaluated_before_observation_maturity`, `test_observation_window_is_fixed_to_final_28_days` |

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
# contract tests (47 direct-mode tests: lifecycle, adversarial, audit regressions)
python -m pytest -q          # needs genlayer-test (gltest); pins GENVM_VERSION in tests/conftest.py

# deploy (CLI already configured with a funded account)
genlayer network set studio-next
genlayer deploy              # runs deploy/deploy_aether_claim.ts

# read checks
genlayer call 0x6Ee88b72fd33A3e6FB475Aae3f966e9b58fa213b list_plots
genlayer call 0x6Ee88b72fd33A3e6FB475Aae3f966e9b58fa213b get_pool
genlayer call 0x6Ee88b72fd33A3e6FB475Aae3f966e9b58fa213b get_config

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
Next — it is independent of GenLayer protocol fees. Reads race the two RPCs
under a 5s limit and fall back to the genesis registry. Wallet-side behaviour
was not exercised against a live MetaMask in this build.
