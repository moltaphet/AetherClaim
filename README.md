# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| Contract | `0xD368f35b238d84Bfd1024441992Cc2d04276bf79` |
| Explorer | https://explorer-studio-next.genlayer.com/address/0xD368f35b238d84Bfd1024441992Cc2d04276bf79 |

> **Deployment status (honest note).** The contract is deployed and its schema
> is served by the RPC (`deployments/aether_claim_abi.json`, 18 methods). At the
> time of writing, Studio's `gen_call` endpoint returned
> `Can't reconnect until invalid transaction is rolled back` for *every*
> contract, including an unrelated one, so the genesis registry and pool state
> could not be read back on-chain and the 0.5 GEN liquidity seed in the deploy
> script did not run. Re-run the read checks below once Studio recovers, then
> `provide_liquidity`.

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

Baselines are expected mm per **14-day window**. A claim observes **two
consecutive windows** (28 days), lagged 6 days behind today for reanalysis
publication.

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

**Known limits.** The 2-of-3 rule means a persistent single-source outage still
settles on two sources; all sources are public weather pipelines (not a
hardware oracle); the admin is a single address (use a multisig); NOAA is
consumed via Open-Meteo's GFS model rather than NWS directly; the premium is a
flat 8% (no actuarial pricing). This is hackathon-grade, not audited.

## 3. Verify

```bash
# contract tests (29 direct-mode tests incl. adversarial + validator checks)
python -m pytest -q          # needs genlayer-test (gltest); pins GENVM_VERSION in tests/conftest.py

# deploy (CLI already configured with a funded account)
genlayer network set studio-next
genlayer deploy              # runs deploy/deploy_aether_claim.ts

# read checks
genlayer call 0xD368f35b238d84Bfd1024441992Cc2d04276bf79 list_plots
genlayer call 0xD368f35b238d84Bfd1024441992Cc2d04276bf79 get_pool
genlayer call 0xD368f35b238d84Bfd1024441992Cc2d04276bf79 get_config

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

Frontend notes: transactions pass `gas: 0x7a120` (500000) and EIP-1559 fee
fields via a provider wrapper (`withGasOverrides`) and a fee preset from
`estimateTransactionFees`; reads race the primary RPC against a 9s timeout and
fall back to the second RPC. Wallet-side behaviour was not exercised against a
live MetaMask in this build.
