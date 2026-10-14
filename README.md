# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| **Contract (canonical)** | **`0x520928106203cb618A92841B1fD0A69978fE6Dc5`** |
| Explorer | https://explorer-studio-next.genlayer.com/address/0x520928106203cb618A92841B1fD0A69978fE6Dc5 |
| Deploy transaction | `0x68722d339d57fbba616b92bb81a26023dd095811575f11ce35b842246a14e0a4` (a transaction hash, not an address) |
| Liquidity seed transaction | `0x7cf0d675c740a59bf9626d32747f6586cb2bdb3ba0ab8cda63e037306e39d89b` (a transaction hash, not an address) |

This is the only active deployment. Earlier iterations of this contract were
deployed to other Studio Next addresses during development. They are
**superseded and deprecated**, are not recorded here on purpose, and must not be
used. `deployments/studio-next.json` always holds the canonical record.

> **Deployment status (honest note).** This is the **round-8** build: validators
> compare `tier` and `source_count` exactly and allow ±5 points of noise on
> `median_deficit`, restoring liveness after round 7. It is deployed to Studio Next with the 0.5 GEN liquidity seed accepted
> (`deployments/studio-next.json`). The served schema matches
> `deployments/aether_claim_abi.json` (19 methods, identical name set). Studio's
> `gen_call` read endpoint has been failing server-side (`Can't reconnect until
> invalid transaction is rolled back`) for every contract, so the on-chain
> genesis registry and pool state have **not been read back**. The frontend
> falls back to cached genesis data until reads recover.

## 1. Architecture

```
            ┌────────────────────── AetherClaim contract ──────────────────────┐
 LP ──GEN──▶│ provide_liquidity        pool_balance / total_liabilities        │
 Farmer ───▶│ create_policy(plot,cov)  plots{} (admin registry, 7-day staging) │
            │   refuses arid + off-season terms   policies{}  epoch_exposure{} │
 Anyone ───▶│ evaluate_claim ──────┐   claims{}                                │
            │                      ▼                                          │
            │   leader: 3 sources → per-source tier (lower of 2 windows)       │
            │           → aggregate: median of 3, lower of 2 → {tier, ...}     │
            │   validators: re-run; tier + count exact, deficit within ±5     │
 Anyone ───▶│ release_payout   24h cooldown → 3-day linear vesting → holder    │
            └──────────────────────────────────────────────────────────────────┘
   Open-Meteo ERA5 archive ── Open-Meteo GFS (NOAA model) ── NASA POWER
   (two legs share one operator: see §5)
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
  days, the reanalysis lag. Evaluation closes at term end + 30 days (the grace
  period).
- **Baselines.** Each window is judged against the baseline of **its own calendar
  month**, taken at the window midpoint: `window baseline = monthly_mm × 14 / 30`.
- **Micro-baseline floor.** A window whose baseline is under **10 mm** cannot
  evidence a drought and reads 0%. The purchase check uses the same window math,
  so a term with any arid window is refused outright (see §2).
- **Per source.** Each source's tier is the lower of its two window tiers. A
  one-window anomaly therefore pays nothing.

### 1b. Source aggregation and who may settle

| Answering sources | Who may settle | Settles at |
|---|---|---|
| 0 or 1 | nobody | `QUORUM_FAILURE`. Retry permitted until grace ends |
| 2 (degraded) | the **policyholder only** | the **lower** of the two tiers |
| 2 (degraded) | anyone else | `QUORUM_FAILURE`. **No state is written** |
| 3 (full quorum) | **any caller** | the **median** of the three tiers |

A split never pays zero by default. It settles at the tier that at least two
answering sources support. The payout is `coverage × tier payout`, paid to the
policyholder, and a tier of 0 settles as `SETTLED_NO_PAYOUT`. Any tier above 0
settles as `SETTLED_PAYOUT` and enters the 24-hour cooldown and 3-day vesting.

**Why degraded settlement is policyholder-only.** A settlement is final. The
degraded tier is never above the full three-source median, so if any caller could
settle a two-source result, a third party (or a griefing LP) could call during a
one-source outage and lock the insured into a lower tier, destroying their ability
to retry once all three sources answer. In round 5 that was possible
(`min(tier_a, tier_b)` was permissionless). Now the insured chooses: wait for the
third source, or accept the lower tier now
(`test_third_party_cannot_force_lower_tier_settlement_on_outage`,
`test_policyholder_can_voluntarily_accept_degraded_two_source_settlement`).

A third party's degraded or sub-quorum call also writes **nothing**, including the
retry timestamp. Otherwise a griefer could keep the one-day retry rate limit
occupied and front-run the insured's retries
(`test_third_party_outage_attempt_does_not_block_policyholder_retry`).

Examples, with per-source tiers listed:

| Sources (ERA5 / GFS / NASA) | Settles at | Payout |
|---|---|---|
| 3 / 3 / 0 | median 3 | 100% |
| 3 / 1 / 1 | median 1 | 30% |
| 3 / outage / 1 | lower of 3, 1 = 1 (policyholder only) | 30% |
| 3 / 0 / outage | lower of 3, 0 = 0 (policyholder only) | 0%. See the limitation in §2 |

> **Infrastructure Note on Quorum:** Both Open-Meteo ERA5 and GFS endpoints share
> the same underlying hosting infrastructure. In this testnet deployment they
> provide independent re-analysis vs forecast datasets, but represent a correlated
> hosting dependency. For mainnet production, the source list should be extended
> with direct enterprise gateways (for example dedicated NOAA enterprise feeds or
> ECMWF APIs) to achieve full infrastructure diversification. None of these are
> integrated yet. The endpoints are built inside the contract, so changing them
> means a new deployment.

**Infrastructure corroboration disclosure.** Both the Open-Meteo ERA5 leg and
the Open-Meteo GFS leg originate from the same underlying hosting infrastructure.
The "three sources" are therefore two infrastructure providers, not three, and a
single host outage or fault can remove or corrupt two legs at once. Before a
mainnet deployment, distinct independent endpoints should be integrated, such as
direct NOAA enterprise feeds and ECMWF API access, to eliminate correlated host
outages. The full discussion is in §5.

**Why the median and not the minimum of three.** The spec allowed "the
conservative lower tier, or the median". The minimum of three is not safe here.
A minimum rises whenever a source drops out, so a policyholder who could see the
dissenting source go down could settle higher. Checked exhaustively over all 64
tier combinations and all 8 availability patterns, a minimum of three would raise
the payout in **42 of 512** cases. The median cannot be raised by availability.
Over the same 512 cases the shipped rule raised it in **0**. See
`test_availability_cannot_raise_payout`.

## 2. Game-theoretic security & anti-exploit design

**Economic non-determinism (TruScore flaw).** Validators never compare
continuous values within a tolerance. Each validator recomputes the aggregated
tier and the validator function requires `leader.tier == validator.tier`
exactly. The payout is `coverage × {0, 3000, 7000, 10000} / 10000`, so any two
validators that accept move identical money. Borderline cases (49% vs 50%
deficit) are *rejected*, not averaged (`test_economic_drift_rejected_by_validator`).

**What validators compare.** The payload must carry exactly the four expected
keys with integer types (checked before any cast). Then the leader's payload and
the validator's own run must match as follows:

| Field | Comparison | Why |
|---|---|---|
| `tier` | exact | it is the economic outcome |
| `source_count` | exact | it decides who may settle: a degraded two-source result is policyholder-only, and a leader must not be able to claim three answering sources when only two did (`test_validator_rejects_forged_source_count_payload`) |
| `median_deficit` | within **±5 points** | telemetry. Only the tier moves money, so forecast-model updates or rounding between validator runs must not fail consensus and starve the policyholder (`test_validator_tolerates_deficit_noise_but_not_drift`) |

The ±5 band is a judgment call, not a derived constant. Drift beyond it (a source
that has moved 11 points inside the same tier) is still rejected. Tier equality
still guarantees both runs saw the same bucket, and `source_count` equality
guarantees they saw the same sources.

**Self-attack / moral hazard (PulseSLA flaw).**
- *No arbitrary coordinates.* `create_policy` accepts only a `plot_id` present
  and active in the registry. Latitude, longitude, baselines and cadastral hash
  are written by the admin after a 7-day staging delay. Observation URLs are
  built *inside the contract* from registry values, so nothing a policyholder
  passes ever reaches a fetch.
- *The insured cannot cause the insured event.* Rainfall is not influenced by
  the policyholder. The remaining attack surface is timing, covered below.
- *Worthless policies are refused.* `create_policy` reverts with
  `Term uninsurable: observation window falls in arid month (< 10mm baseline)`
  if **either** final window's baseline is under the floor. A policy that could
  only settle at 0% is never sold (`test_arid_term_reverts_at_purchase`).
- *Single-station variance.* One faulty station cannot erase a claim that two
  others support (`test_single_low_outlier_is_outvoted`). One faulty high reading
  cannot inflate a payout past what two sources support
  (`test_single_station_fault_cannot_trigger_payout`).

**Fixed-window settlement and timing.** `observation_start` and
`observation_end` are recorded at purchase, so no evaluation time can move the
windows. A policyholder cannot choose which weather days are judged, which is the
American-style exercise that options markets guard against. The verdict is taken
once: a settled outcome is final, and a second evaluation is refused.

The remaining timing levers, and how far they go:

- **Availability cannot raise the payout, and a degraded tier is never above the
  full median.** The aggregate is an order statistic, so dropping a source can
  only lower the settled tier. That is why waiting for the third source is weakly
  better for the insured, and why only the insured may accept a degraded tier. The
  exhaustive sweep proves the monotonicity (`test_availability_cannot_raise_payout`).
- **Data revisions can still move a retry.** If a source revises a historical
  value during the grace period, a retry can read a different number. The
  contract does not pin data versions. Settled outcomes are final, so only a
  retry after an outage is exposed. See *Known limits*, item 2.

**Solvency & timing.**
- Per-epoch (7d) coverage cap `max_epoch_exposure`, single-policy
  concentration cap (25% of pool), and `total_liabilities ≤ pool_balance`.
- LPs can only withdraw capital not reserved against live policies.
- Approved payouts wait 24h, then vest linearly over 3 days via
  `release_payout` (effects before transfer).
- **Outage tolerance.** A quorum failure (fewer than 2 sources) stays `ACTIVE`.
  A retry is allowed once a day until **30 days after term end**
  (`test_quorum_failure_retry_then_settlement_is_final`).
- **No retry hold.** Once the grace period ends, the reserve can be released at
  once. There is no extra hold after the last attempt
  (`test_quorum_failure_retries_then_releases_at_grace_end`). LP capital is not
  locked beyond the grace period.
- **Orphan guard.** The last LP cannot exit while policies are live
  (`test_last_lp_cannot_orphan_reserved_capital`). Burning every share would
  strand the reserve with no shareholder, and the next depositor would inherit
  it. The accounting guard in `provide_liquidity` fails closed on any state where
  capital and shares disagree. Those states are unreachable through the public
  API, because any surviving share keeps at least 1 wei of free capital.

**Why conservative lower-bound settlement beats all-or-nothing.** An earlier
design paid zero on any disagreement between sources. The conservative rule
replaces that cliff with a tier that at least two sources support. The argument
is about how the realised payout tracks the risk the premium priced:

- A premium prices the expected payout from the weather. Under all-or-nothing, the
  realised payout also depends on whether one station disagrees on a bucket edge,
  which is a risk the premium never priced. Two sources reading a severe drought
  (2 / 2 / 0) pay **0%** under all-or-nothing, but **70%** under the median.
- The median and the lower-of-two both tie the payout to a reading that a
  majority of answering sources supports. Every settled tier is backed by at
  least two answering sources reading that tier or higher. This was checked
  exhaustively, with 0 violations.
- LPs are still protected. No single high-reading outlier can raise the payout,
  and the lower-of-two rule is conservative, though it is only reachable if the
  policyholder accepts it.

**Known limits.** Read these before relying on the product.

1. **Two-source settlement can be wiped by one reading, but only by the insured's
   choice.** With two sources answering, the lower reading governs. A drought read
   by one source and normal rain from the other settles at zero if the
   policyholder accepts it (`test_two_source_split_settles_at_lower_reading_even_if_zero`).
   The "no wipe-out" guarantee holds in full only when three sources answer. A
   policyholder who waits is never worse off in tier terms.
2. **Data revisions.** The contract assumes the historical values inside the fixed
   windows are stable through the grace period. A revised value can change a
   retry's result, for example by pulling a median toward a revised source.
   Settled outcomes are final, so the exposure is limited to retries after
   outages. Mitigations, none implemented: a keeper that evaluates at a fixed
   time, or pinning the source snapshot on-chain.
   **ERA5 re-analysis.** The ERA5 leg is a reanalysis product. ECMWF publishes
   preliminary data within days and may later replace it with the validated
   dataset, typically weeks to months afterwards. A value read at maturity can
   therefore differ slightly from a later read. The fixed maturity is the safest
   anchor available: windows are fixed at purchase, evaluation opens at a known
   instant, and the first settlement is final, so a later ECMWF revision cannot
   reopen a settled policy. It also means a settled outcome rests on the
   preliminary figure.
3. **Shared operator (see §5).** Two of the three legs are Open-Meteo. They are
   not independent.
4. **Admin ban binds one address, not a person.** An admin can still use a second
   wallet. Use a multisig for the admin, and watch registry changes (plots are
   public for 7 days before activation).
5. **Capital is locked for the grace period.** Expired-but-unevaluated policies
   keep their reserve until day 30 after term end, then release at once.
6. **Weather sources are public APIs, not a hardware oracle.** NASA POWER is a
   reanalysis product with a publication lag. The premium is a flat 8% with no
   actuarial pricing.
7. **Strict tier equivalence vs. boundary liveness.** `median_deficit` has a ±5
   point telemetry tolerance, but `tier` is compared exactly. If two validators
   read values that straddle a tier boundary (49% is Tier 1, 50% is Tier 2),
   they disagree on the tier, consensus fails, and the leader rotates. This is an
   intentional trade-off. It puts zero economic drift ahead of boundary liveness,
   which is the steward's core requirement that no single policy can settle at
   divergent payouts. The failure mode is a stall, never a split payout. A stall
   is only possible when validators read different values, and it is bounded by
   the grace period. If the disagreement persisted to day 30, the evaluation
   window would close and the policy would expire without a payout, so a policy
   whose reading sits on a boundary carries that risk. A bucketed or tolerant
   tier comparison would remove the stall but would reintroduce divergent
   payouts, so it is not offered.
8. **Not audited.** This is hackathon-grade code.

## 2b. Audit remediation

| # | Finding | Fix |
|---|---|---|
| 1 | One scalar baseline made every dry season look like a drought | **Monthly actuarial baselines** (12 values) + validated crop cycle. `create_policy` rejects any term that touches an off-season month; each window is compared with its own month's baseline. Regression: `test_seasonal_summer_exploit_prevented`, `test_per_month_baselines_drive_the_deficit` |
| 2 | Admin could stage a plot, insure it, and self-pay | `create_policy` reverts for the admin address; plot activation timelock is **7 days**; `set_max_epoch_exposure` is bounded. Regression: `test_admin_self_insurance_prevented` |
| 3/4 | Flat principal accounting: premiums unowned, losses fall on the last LP out | **ERC-4626-style share vault**: shares priced against `pool_balance` before the deposit lands; `withdraw_liquidity(shares)` pays `shares × pool_balance / total_shares`. Premiums raise share price, and paid claims lower it for all LPs equally. Regression: `test_lp_earns_accumulated_premiums`, `test_lp_share_fair_loss_distribution` |
| 5 | Validators only compared `tier`; leader-supplied fields were stored unchecked | Validators reject any payload that fails strict structure and bounds checks (`tier ∈ {−2} ∪ [0,3]`, `0 ≤ median_deficit ≤ 100`, ints not bools, quorum rules, deficit inside the tier bucket, printable ≤ 400-char reasoning). The contract re-checks before writing and **stores a reasoning string it builds itself**. Regression: `test_validator_rejects_malformed_leader_payload` |
| 6 | Last LP burns 100% of shares while the reserve is live: capital is orphaned | `withdraw_liquidity` reverts when `total_lp_shares − burn == 0` and `total_liabilities > 0`. `provide_liquidity` fails closed on capital-without-shares or shares-without-capital. Regression: `test_last_lp_cannot_orphan_reserved_capital` |
| 7 | Arid windows (0.9 mm expected) read 100% on trivial shortfalls | Windows with baseline under `MIN_INSURABLE_BASELINE_MM = 10` read 0% at settlement |
| 8 | Caller could evaluate on any day, choosing the best window | European exercise: windows fixed at purchase to the term's final 28 days, verdict only at `matures_at`, one final settlement. Regression: `test_claim_cannot_be_evaluated_before_observation_maturity`, `test_observation_window_is_fixed_to_final_28_days` |
| 9 | Arid terms could be sold with a guaranteed 0% payout | `create_policy` refuses any term whose final window is under the floor, using the settlement's window math. A monthly check would still sell 10–21 mm months whose windows are under the floor. Regression: `test_arid_term_reverts_at_purchase`, `test_arid_control_plot_still_insurable` |
| 10 | Central Valley had Apr–Oct as its cycle, which is dormant and dry | Cycle set to **Nov–Mar** with Mediterranean winter baselines. Regression: `test_central_valley_winter_policy_insurable_and_settles`, `test_central_valley_summer_term_rejected`, `test_central_valley_purchase_calendar` |
| 11 | A 14-day grace let third-party outages forfeit a valid claim | Grace raised to **30 days**. A quorum failure stays retryable until the grace ends. Regression: `test_quorum_failure_retry_then_settlement_is_final`, `test_evaluation_closes_after_grace`, `test_expire_releases_reserve_after_grace` |
| 12 | A source disagreement wiped out the claim (`INCONCLUSIVE_DISAGREE`, 0%) | **Conservative lower-bound settlement.** Three answering sources settle at the median tier, permissionlessly. Two answering settle at the lower tier, but **only the policyholder may accept it**, so a third party cannot lock the insured into a lower tier (see rows 16 and 17). Only fewer than two answering, or a degraded call by anyone else, is a `QUORUM_FAILURE`. Regression: `test_source_disagreement_settles_at_conservative_lower_tier`, `test_three_source_split_settles_at_median`, `test_single_low_outlier_is_outvoted` |
| 13 (round 5) | Retry-after-split could wait for the dissenting source to go down | Closed by monotonicity, not by blocking splits. The aggregate can only fall when a source drops, so availability can never raise a payout above the full median (the sweep covers 512 cases). Round 6 relies on this to make degraded settlement the insured's choice. Regression: `test_availability_cannot_raise_payout` (512 cases) |
| 14 (round 5) | A 14-day hold after each outage retry extended lock-up with no benefit | Hold removed. Expiry releases once the grace period ends. Regression: `test_quorum_failure_retries_then_releases_at_grace_end`, `test_grace_and_aggregation_config` |
| 15 (round 5) | Docstrings did not state the timing assumptions for quorum retries | `evaluate_claim` and `_corroborate` now state the assumptions: fixed windows, one final settlement, retry only on outage, monotone availability, and the data-stability assumption |
| 16 (round 6) | Any caller could settle a two-source result at `min(a, b)` during a one-source outage, locking the insured into a lower tier and removing their retry | Two-source (degraded) settlement is **policyholder-only**. Others get `QUORUM_FAILURE` and no state is written, so they cannot lock a tier or consume the retry rate limit. Three-source settlement stays permissionless. Regression: `test_third_party_cannot_force_lower_tier_settlement_on_outage`, `test_three_sources_settle_permissionlessly_at_median`, `test_policyholder_can_voluntarily_accept_degraded_two_source_settlement`, `test_third_party_outage_attempt_does_not_block_policyholder_retry` |

| 17 (round 7) | Validators compared tier only, so a leader could forge `source_count: 3` on a two-source result and let a third party settle a degraded tier | Validators compare `tier` and `source_count` exactly, and require exactly the four payload keys. Regression: `test_validator_rejects_forged_source_count_payload` |
| 18 (round 8) | Comparing `median_deficit` exactly (round 7) made validators fail consensus on harmless numeric noise, such as a GFS model update or rounding, starving policyholders | `median_deficit` is compared within **±5 points** (`DEFICIT_NOISE_TOLERANCE`). `tier` and `source_count` stay exact, so neither the payout nor the settle permission can be forged. Regression: `test_validator_tolerates_deficit_noise_but_not_drift` |
**Deliberate deviation from the plain ERC-4626 formula.** A withdrawal pays
`min(shares × pool / total_shares, shares × (pool − reserved) / total_shares)`.
With only the first term, an LP could exit while a claim is still unrecognised
and leave the remaining LPs holding the loss. The cap means reserved capital never
leaves ahead of the claims it backs. `test_lp_share_fair_loss_distribution` proves
the leaver and the stayer end with identical value.

## 3. Verification & Testing

### 3a. Reproducing the 67 direct-mode contract tests

The suite runs the contract in the **direct VM execution mode** of
`genlayer-test` (gltest). The contract's Python executes in-process under the
GenVM SDK, with **mocked HTTP adapters** (`direct_vm.mock_web`) standing in for
the three weather sources, and `direct_vm.warp` for the block clock. That isolates
the economic logic from testnet flakiness, including the Studio database-session
errors that currently break `gen_call` reads. It also means the suite does not
exercise a live validator network, real source data or the wallet.

**Prerequisites**

| Requirement | Value |
|---|---|
| Python | 3.12 (verified with 3.12.12) |
| Test framework | `genlayer-test==0.30.0rc2` (pulls `genlayer-py` 0.19.0rc2 and `pytest`) |
| GenVM runner | `GENVM_VERSION=v0.6.0-rc5`, set by default in `tests/conftest.py` |
| Network | needed once, to download the runner bundle from GitHub releases |
| Disk | the runner is cached under `~/.cache/gltest-direct` |

The contract's pinned runner (`py-genlayer:5jycge…`) ships in the `v0.6.0-rc5`
tree. Without that version, gltest resolves the *latest* GenVM release and the
runner is not found, so the pin matters.

**Commands** (verified from an empty cache and a fresh virtualenv: 67 passed in
2m26s, almost all of it the one-time runner download. A warm cache runs in about
5 s.)

```bash
# 1. virtualenv on Python 3.12
uv venv --python 3.12 .venv

# 2. install gltest. genlayer-py is a pre-release, so uv needs the flag
uv pip install --python .venv/bin/python --prerelease=allow -r requirements-dev.txt

# 3. run the suite (GENVM_VERSION defaults to v0.6.0-rc5 via tests/conftest.py)
.venv/bin/python -m pytest tests/test_aether_claim.py -v

# equivalent, with the activated environment
source .venv/bin/activate
python -m pytest tests/test_aether_claim.py -v
```

To set the runner version explicitly: `GENVM_VERSION=v0.6.0-rc5 python -m pytest tests/test_aether_claim.py -v`.

Notes:
- With plain `pip`, `pip install -r requirements-dev.txt` should resolve the
  pre-release dependency on its own, but the `pip` and stdlib-`venv` route was
  **not** verified on this machine (its Python 3.12 is a `uv` build without
  `ensurepip`).
- A `HTTP Error 403: rate limit exceeded` warning while resolving the latest
  GenVM version is harmless: the pinned version is used.
- Use Python 3.12. The suite was not run on the system Python 3.14.

### 3b. Other checks

```bash
# deploy (CLI already configured with a funded account)
genlayer network set studio-next
genlayer deploy              # runs deploy/deploy_aether_claim.ts

# read checks (currently failing on Studio; see the deployment note)
genlayer call 0x520928106203cb618A92841B1fD0A69978fE6Dc5 list_plots
genlayer call 0x520928106203cb618A92841B1fD0A69978fE6Dc5 get_pool
genlayer call 0x520928106203cb618A92841B1fD0A69978fE6Dc5 get_config

# frontend
cd frontend && npm install && npm run build && npm run dev
```

## 4. Insurable calendar: Central Valley (Almond / winter crop)

The genesis plot `CV-ALMOND-01` has a crop cycle of **November through March**
(months 11, 12, 1, 2, 3). Its monthly rainfall baselines (mm, Jan→Dec) are:

| Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 45 | 35 | 15 | 8 | 2 | 0 | 0 | 4 | 15 | 25 | 45 |

Those values are the Mediterranean winter pattern supplied for this round. They
are **illustrative and not checked against station normals**. Validate them
before real money is involved.

**Purchase window.** A 90-day term must lie entirely inside the cycle, so
purchases are accepted from **1 November through 31 December** each year:

- Starting **1 Nov** gives a term ending **30 Jan**, with windows in January.
- Starting **31 Dec** gives a term ending **31 Mar**, with windows in March.
- Starting **1 Jan** reaches April, which is off-season, so the purchase reverts.

All cycle windows clear the 10 mm floor (Nov 11.7 mm, Dec 21.0, Jan 23.3, Feb
21.0, Mar 16.3 per window). The calendar does the work here, not the floor. The
floor protects other plots and any future change to the data. The frontend shows
the next insurable start date for the selected plot.

## 5. Shared infrastructure disclosure

Two of the three legs are served by the same operator. This is not independent
redundancy, and it matters for the aggregation rule.

| Leg | Endpoint | Operator |
|---|---|---|
| ERA5 archive | `archive-api.open-meteo.com/v1/archive` | Open-Meteo |
| GFS forecast (NOAA model) | `api.open-meteo.com/v1/forecast?models=gfs_seamless` | Open-Meteo |
| NASA POWER | `power.larc.nasa.gov/api/temporal/daily/point` | NASA Langley |

Consequences:

- **Two votes, one operator.** When both Open-Meteo legs agree, they decide the
  median of three against NASA POWER alone. Two Open-Meteo legs at Tier 3 and
  NASA at Tier 0 settle at Tier 3 (`test_single_low_outlier_is_outvoted`). This is
  the intended "outvote a single faulty source" behaviour, but it means the two
  legs are not three independent sources.
- **Outage concentration.** An Open-Meteo outage removes two legs at once. Only
  NASA POWER answers, which is below quorum, so claims enter retry until the grace
  period ends.
- **Compromise or bug.** A wrong unit or a compromised Open-Meteo service feeds
  two agreeing values into the median, and the protocol cannot detect it from
  inside.

Production should diversify the GFS leg to a direct NOAA source (NOMADS/NWS) on
an independent host, and add an independent archive such as Copernicus CDS
(ERA5 direct). NASA POWER stays as the third leg.

## 6. Layout

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
under a 5s limit and fall back to the genesis registry, and balances or counts
are never rendered as zero when a read failed (a sync alert shows instead).
Wallet-side behaviour was not exercised against a live MetaMask in this build.

Transaction checks: `ACCEPTED` is not treated as success. The receipt's execution
result must be `FINISHED_WITH_RETURN`. A raised contract or a consensus
disagreement shows "Transaction Reverted on-chain". A receipt that reports no
execution result shows a warning, never a success. Amount inputs reject empty,
zero, negative, signed, scientific and non-numeric entries before any
transaction is built. Weather fetches use a 6 s abort timeout, and a station
that returns a null, missing or negative day is marked `DATA_ERROR` and excluded
from the quorum view rather than read as 0 mm.

**Dependency audit.** `npm audit` reports 6 findings: 1 critical and 5 high.
None can be resolved without a breaking major upgrade. Only `next` is a
runtime dependency. The other five are build-time tooling.

| Package | Severity | Kind | Why it is still flagged |
|---|---|---|---|
| `next` 14.2.35 | critical | direct, runtime | 23 advisories (2 critical, 8 high, 11 moderate, 2 low): RCE in the image optimizer with AVIF, DoS in Server Components and Server Actions, SSRF in rewrites, cache poisoning. Fixed only in Next 16 |
| `tailwindcss` 3.4.19 | high | direct, **devDependency** | flagged through the three packages below. Fixed only in Tailwind 4 |
| `chokidar` 3.6.0 | high | transitive of tailwindcss | depends on `braces` |
| `micromatch` 4.0.8 | high | transitive of tailwindcss | depends on `braces` |
| `fast-glob` 3.3.3 | high | transitive of tailwindcss | depends on `micromatch` |
| `braces` 3.0.3 | high | transitive of tailwindcss | the root cause: stack exhaustion on deeply nested glob patterns |

The glob chain is **not fixable with `overrides`**: `braces`, `micromatch` and
`fast-glob` are already at their latest releases, and the advisory range for
`braces` and `fast-glob` is `*`, so no patched version exists to pin. It is
build-time only and its input is Tailwind's own fixed `content` globs.

`package.json` `overrides` do pin `postcss` to 8.5.30 and
`postcss-selector-parser` to 7.1.6, which cleared the earlier postcss findings
(9 findings became 6). The built CSS is byte-identical before and after.

For the `next` findings: the app is client-rendered and uses no `next/image`,
middleware, rewrites, Server Actions or CSP nonces, and the image optimizer is
disabled in `next.config.mjs`. For a production deployment, serve it as a static
export or upgrade Next.js.

**Numeric parity.** The frontend's telemetry preview uses the contract's exact
arithmetic: per-day rainfall to tenths with round-half-to-even (Python's `round`,
not `Math.round`), integer floor division for baselines and deficits, and the
same 10 mm floor. This was checked against the contract's own Python functions on
9,401 rounding inputs (375 of which differ from half-up rounding) and 90,930
deficit and tier boundary cases, with no mismatches.

**Account switching.** Panels are keyed by the connected account, so switching
accounts unmounts them and no policy, claim, share balance or notice from the
previous address can be shown for the new one. Responses that arrive after a
switch are discarded.
