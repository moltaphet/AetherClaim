# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| Contract | `0x9066D38A81FE5fE84b10c8dF9C4557b04bC26D83` |
| Explorer | https://explorer-studio-next.genlayer.com/address/0x9066D38A81FE5fE84b10c8dF9C4557b04bC26D83 |

> **Deployment status (honest note).** This is the **audited** build
> (monthly baselines, LP share vault, admin ban), deployed to Studio Next with
> the 0.5 GEN liquidity seed accepted (`deployments/studio-next.json`). The
> schema is served by the RPC and matches `deployments/aether_claim_abi.json`
> (19 methods). At deploy time Studio's `gen_call` read endpoint returned
> `Can't reconnect until invalid transaction is rolled back` for every
> contract, so the genesis registry and pool state were **not read back
> on-chain**; the frontend falls back to cached genesis data until reads
> recover. Run the read checks in section 3 to confirm.

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

A claim observes **two consecutive 14-day windows** (28 days), lagged 6 days
behind today for reanalysis publication. Each window is judged against the
baseline of **its own calendar month** (the month of the window midpoint):
`window baseline = monthly_mm × 14 / 30`. A plot carries 12 monthly baselines
and a crop cycle (`start_month`..`end_month`, may wrap the year).

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
  the policyholder. The attack surface that remains is information (buying
  after the drought) — closed by requiring both observation windows to sit
  inside the coverage period (`test_policy_bought_after_drought_cannot_claim`).
- *Single-source / isolated fault.* ≥ 2 of 3 independent sources must return
  HTTP 200 with complete data and **all** answering sources must land in the
  same tier; otherwise the round is `INCONCLUSIVE` and pays 0.
- *One-off anomaly.* The tier is the minimum over two consecutive windows.

**Solvency & timing.**
- Per-epoch (7d) coverage cap `max_epoch_exposure`, single-policy
  concentration cap (25% of pool), and `total_liabilities ≤ pool_balance`.
- LPs can only withdraw capital not reserved against live policies.
- Approved payouts wait 24h, then vest linearly over 3 days via
  `release_payout` (effects before transfer).
- Evaluations are rate-limited to one per day per policy; an approved claim is
  terminal.

**Known limits.** The admin ban binds one address, not a person: an admin can
still use a second wallet, so the admin should be a multisig and registry
changes should be watched (plots are public for 7 days before activation). The
2-of-3 rule means a persistent single-source outage still
settles on two sources; all sources are public weather pipelines (not a
hardware oracle); the admin is a single address (use a multisig); NOAA is
consumed via Open-Meteo's GFS model rather than NWS directly; the premium is a
flat 8% (no actuarial pricing). This is hackathon-grade, not audited.

## 2b. Audit remediation (institutional hardening)

| # | Finding | Fix |
|---|---|---|
| 1 | One scalar baseline made every dry season look like a drought | **Monthly actuarial baselines** (12 values) + validated crop cycle. `create_policy` rejects any term that touches an off-season month; each window is compared with its own month's baseline. Regression: `test_seasonal_summer_exploit_prevented`, `test_per_month_baselines_drive_the_deficit` |
| 2 | Admin could stage a plot, insure it, and self-pay | `create_policy` reverts for the admin address; plot activation timelock is now **7 days**; `set_max_epoch_exposure` is bounded. Regression: `test_admin_self_insurance_prevented` |
| 3/4 | Flat principal accounting: premiums unowned, losses fall on the last LP out | **ERC-4626-style share vault**: shares priced against `pool_balance` before the deposit lands; `withdraw_liquidity(shares)` pays `shares × pool_balance / total_shares`. Premiums raise share price, paid claims lower it for all LPs equally. Regression: `test_lp_earns_accumulated_premiums`, `test_lp_share_fair_loss_distribution` |
| 5 | Validators only compared `tier`; leader-supplied fields were stored unchecked | Validators reject any payload that fails strict structure/bounds (`tier ∈ [-1,3]`, `0 ≤ median_deficit ≤ 100`, ints not bools, quorum ≥ 2 for a settled tier, deficit inside the tier bucket, printable ≤ 400-char reasoning). The contract re-checks before writing and **stores a reasoning string it builds itself**, so no leader text reaches state. Regression: `test_validator_rejects_malformed_leader_payload` |

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
# contract tests (44 direct-mode tests: lifecycle, adversarial, audit regressions)
python -m pytest -q          # needs genlayer-test (gltest); pins GENVM_VERSION in tests/conftest.py

# deploy (CLI already configured with a funded account)
genlayer network set studio-next
genlayer deploy              # runs deploy/deploy_aether_claim.ts

# read checks
genlayer call 0x9066D38A81FE5fE84b10c8dF9C4557b04bC26D83 list_plots
genlayer call 0x9066D38A81FE5fE84b10c8dF9C4557b04bC26D83 get_pool
genlayer call 0x9066D38A81FE5fE84b10c8dF9C4557b04bC26D83 get_config

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
