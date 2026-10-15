# AetherClaim — Decentralized Parametric Climate & Agricultural Insurance

Parametric drought cover on GenLayer. Validators settle claims by independently
re-querying three meteorological pipelines and agreeing on a **discrete payout
tier**. No LLM is in the settlement path: the verdict is a pure function of
observed rainfall.

| | |
|---|---|
| Network | GenLayer Studio Next, chain `61997` |
| RPC | `https://studio-next.genlayer.com/api` (fallback `studio-dev.genlayer.com/api`) |
| **Contract (canonical)** | **`0xA4c1eCC531d13486487827D40437FBF1E3CB8F2b`** |
| Explorer | https://explorer-studio-next.genlayer.com/address/0xA4c1eCC531d13486487827D40437FBF1E3CB8F2b |
| Source SHA-256 | `d56edd562a293f2f03f306d2ab2b676ffc79c933a8f4a4255e32eb6b0c56ca7f` (`contracts/aether_claim.py`) |
| Exact deployed source | https://github.com/moltaphet/AetherClaim/blob/6d742cfbf3f6485ff3ee63c1fb69a85488d95213/contracts/aether_claim.py (SHA-256 above) |
| Deploy transaction | `0x822550426ce5de576896b116fc2f01a2e11060c4eb65c56ea9b26d47380fe77f` |
| Plot provenance transactions | see `deployments/studio-next.json` → `provenanceTxs` |
| Liquidity seed transaction (0.5 GEN) | `0x61795a2afde8b130569d36318342497670192adf4432ff9d2016d8f12737f93d` |
| Live policy transaction (`create_policy`) | `0xc40ccc69f140e90e65aaeb0764bad7fe324cbef2c1c9a130d1b87abfed0954d0` |
| Cadastral manifest (pinned) | `registry/cadastral_manifest.json` @ commit `3b39beb` |

This is the only active deployment. Earlier iterations of this contract were
deployed to other Studio Next addresses during development. They are
**superseded and deprecated**, are not recorded here on purpose, and must not be
used. `deployments/studio-next.json` always holds the canonical record.

> **Deployment status.** This is the steward-round build, deployed to Studio Next
> and exercised live (`deployments/studio-next.json`):
>
> - **Plot provenance verified on-chain.** `verify_plot_provenance` was executed
>   for all three genesis plots (`0x972332bc3f…`, `0x95b2922f8f…`,
>   `0xd40e9fb224…`). A `list_plots` readback shows
>   `provenance_verified: true` for each, with `provenance_manifest_sha256`
>   `08adb6e9…` equal to the SHA-256 of `registry/cadastral_manifest.json`.
> - **Liquidity.** 0.5 GEN seeded: `pool_balance` 0.5 GEN, `total_lp_shares` 0.5e18.
> - **Live policy.** `P-1`, 0.1 GEN coverage on `AND-OLIVE-01`, signed by the
>   registered operator (`0xbE43…2B36`), premium 0.008 GEN. Readback:
>   `status: ACTIVE`, plot `coverage_locked: true`, pool `total_liabilities` 0.1 GEN.
> - **Not exercised live:** `evaluate_claim`, `resolve_boundary_dispute`,
>   `re_evaluate_revised_claim` and payout release. A policy only matures 100 days
>   after purchase (`matures_at` 1800662400), so these are covered by the direct-mode
>   suite only. The live validator network has **not** been observed settling a claim.
>
> **Correction to an earlier claim.** Earlier versions of this README blamed
> Studio's `gen_call` for failing plot reads. The real cause was in the contract:
> `get_plot`/`list_plots` returned a Python `float` (`latitude`), which GenVM cannot
> calldata-encode. It was found during this round's first live deploy, fixed
> (coordinates are now decimal strings), and is covered by
> `TestViewReturnsAreCalldataEncodable`. Direct-mode tests never encode return
> values, which is why 67 passing tests did not catch it. Because of it a first
> deployment was superseded (recorded in `deployments/studio-next.json`).

## 0. Steward review: requirement → change → evidence

The hackathon steward marked the previous build "Action needed". Each item is
answered below, with the test that proves it and what is **not** solved.

| Steward requirement | What changed | Test / evidence | Honest limit |
|---|---|---|---|
| Independently verifiable plot provenance | `verify_plot_provenance(plot_id)`: validators fetch a manifest pinned to a **git commit** and match coordinates, 12 baselines, crop cycle, `max_plot_coverage`, GHCN station and eligible operator, and the stored `cadastral_hash` must be the SHA-256 of that record. Staging refuses a hash that does not commit to the record. A plot is not insurable until verified (`ERR_PROVENANCE_UNVERIFIED`) | `test_unverified_plot_provenance_cannot_be_insured` (9 field-level mismatches, outage, bad schema, forged validator payload), `test_cadastral_hash_must_commit_to_the_staged_record`, `test_genesis_plots_verify_against_independent_manifest`, `test_committed_manifest_file_verifies_every_genesis_plot` | The manifest is a file in this project's own GitHub repository. Pinning it to a commit makes it immutable and independently re-checkable, but it is **not a government land registry**, and the admin who wrote the plots also wrote the manifest. See "Provenance trust model" below |
| Policyholder eligibility | Each plot binds one `eligible_policyholder`, carried in the manifest. `create_policy` reverts with `ERR_INELIGIBLE_POLICYHOLDER` for anyone else | `test_ineligible_policyholder_rejected_on_create_policy` | One operator per plot, set at staging. There is no on-chain operator change |
| No duplicate coverage on a plot | One live coverage window per plot, and unresolved coverage on a plot may not exceed `max_plot_coverage`. Both revert with `ERR_DUPLICATE_PLOT_COVERAGE`. Settlement and expiry release the plot's coverage | `test_duplicate_overlapping_coverage_on_same_plot_rejected`, `test_expiry_releases_plot_coverage` | The lock is per `plot_id`. Two registry entries for the same physical parcel are prevented only by the manifest review, not by geometry |
| Correlated weather sources | Three sources on **three hostnames and three operator domains**: Open-Meteo ERA5 (`archive-api.open-meteo.com`), NASA POWER (`power.larc.nasa.gov`), NOAA NCEI GHCN-Daily rain gauge (`www.ncei.noaa.gov`). The contract refuses to run a plan that repeats a hostname **or** an operator domain | `test_three_weather_sources_enforce_distinct_hostnames`, `test_contract_refuses_a_correlated_source_plan`, `test_station_gauge_is_a_real_third_vote` | ERA5 and NASA POWER both assimilate satellite and station data: independent infrastructure, not independent physics. The gauge can be tens of km from the plot (see §5) |
| Persistent disagreement at tier boundaries | A validator accepts a leader whose deficit is within ±5 points and whose tier is **not above its own**. Validators on both sides of a threshold converge on the lower tier. Backstop: `resolve_boundary_dispute` settles at the guaranteed lower bound `tier(deficit − 5)` | `test_validators_straddling_tier_boundary_converge_without_liveness_deadlock`, `test_boundary_dispute_resolves_at_guaranteed_lower_bound`, `test_boundary_dispute_outage_is_retryable_not_final` | This lets a leader understate a tier by one step when the validator's own deficit is within 5 points above a threshold. Always toward LPs, bounded to ±5 points. See §2 |
| Fair, recoverable claims under revisions | Evaluation opens **10 days** after the last observed day. During the 24h payout cooldown `re_evaluate_revised_claim` re-reads all three sources and re-prices the payout up or down. The unpaid part of the coverage stays reserved until the cooldown ends, so an upward revision is always fundable. `finalize_claim` releases it | `test_revised_observations_during_cooldown_fairly_update_settlement_tier`, `test_revision_noise_does_not_flip_the_payout`, `test_revision_needs_all_three_sources`, `test_revision_window_closes_when_streaming_begins`, `test_revision_count_is_bounded`, `test_maturity_lag_is_ten_days_after_the_last_observed_day` | Revisions after streaming begins are not recoverable. A revision needs all three sources, a tier change **and** a deficit move of more than 5 points |
| Live deployment, liquidity, policy evidence, readbacks | See "Live evidence" below | `deployments/studio-next.json` | See the readback note below |

## 1. Architecture

```
            ┌────────────────────── AetherClaim contract ──────────────────────┐
 Anyone ───▶│ verify_plot_provenance   validators ⇄ pinned cadastral manifest │
 LP ──GEN──▶│ provide_liquidity        pool_balance / total_liabilities        │
 Operator ─▶│ create_policy(plot,cov)  verified + eligible + no overlap/cap    │
            │   refuses arid + off-season terms   policies{}  epoch_exposure{} │
 Anyone ───▶│ evaluate_claim ──────┐   claims{}                                │
            │                      ▼                                          │
            │   leader: 3 sources → per-source tier (lower of 2 windows)       │
            │           → aggregate: median of 3, lower of 2 → {tier, ...}     │
            │   validators: count exact, deficit ±5, tier not above their own  │
 Holder ───▶│ resolve_boundary_dispute  guaranteed lower bound tier(d − 5)     │
 Anyone ───▶│ re_evaluate_revised_claim re-price inside the 24h cooldown       │
 Anyone ───▶│ release_payout   24h cooldown → 3-day linear vesting → holder    │
            └──────────────────────────────────────────────────────────────────┘
   Open-Meteo ERA5 ── NASA POWER ── NOAA NCEI GHCN-Daily gauge  (3 operators)
   manifest: raw.githubusercontent.com @ pinned commit
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
- **Maturity.** The verdict may be taken only from `matures_at` = the end of the
  last observed day + `MATURITY_LAG_DAYS` (**10 days**), so preliminary
  satellite and reanalysis estimates have settled. Evaluation closes at term end
  + 30 days (the grace period).
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

| Sources (ERA5 / NASA / NCEI gauge) | Settles at | Payout |
|---|---|---|
| 3 / 3 / 0 | median 3 | 100% |
| 3 / 1 / 1 | median 1 | 30% |
| 3 / outage / 1 | lower of 3, 1 = 1 (policyholder only) | 30% |
| 3 / 0 / outage | lower of 3, 0 = 0 (policyholder only) | 0%. See the limitation in §2 |

**Source independence.** The three sources are served by three different
operators on three different hostnames, and the contract enforces it with
`_sources_independent`: every URL must be `https`, the three hostnames must be
pairwise distinct, **and** the three registrable operator domains must be
pairwise distinct (`open-meteo.com`, `nasa.gov`, `noaa.gov`). Checking hostnames
alone is not enough: the previous design used `archive-api.open-meteo.com` and
`api.open-meteo.com`, which are two hostnames of one operator.
`test_three_weather_sources_enforce_distinct_hostnames` shows the old plan is
rejected. The constructor also refuses to deploy a plan that fails the check.
The full discussion, including what independence does and does not buy, is in §5.

**Why the median and not the minimum of three.** The spec allowed "the
conservative lower tier, or the median". The minimum of three is not safe here.
A minimum rises whenever a source drops out, so a policyholder who could see the
dissenting source go down could settle higher. Checked exhaustively over all 64
tier combinations and all 8 availability patterns, a minimum of three would raise
the payout in **42 of 512** cases. The median cannot be raised by availability.
Over the same 512 cases the shipped rule raised it in **0**. See
`test_availability_cannot_raise_payout`.

## 2. Game-theoretic security & anti-exploit design

**Economic non-determinism (TruScore flaw) and boundary liveness.** The payout
is `coverage × {0, 3000, 7000, 10000} / 10000`, so every accepted evaluation
moves one of four fixed amounts. Earlier rounds compared `tier` for exact
equality, which was safe against drift but had a liveness hole: two honest
validators whose readings straddle a threshold (49% is Tier 1, 50% is Tier 2,
both inside the ±5 telemetry band) rejected each other's leaders forever, and the
policy could expire unpaid. The steward flagged this.

The rule now is **monotone**. A validator accepts a leader payload when

1. `source_count` is equal (it decides who may settle),
2. `|leader.median_deficit − mine.median_deficit| ≤ 5`, and
3. `leader.tier ≤ mine.tier`, where `leader.tier` is the tier of the leader's own
   certified deficit (checked by `_payload_ok`).

A leader **above** a validator's own tier is still rejected, so no accepted
result is ever higher than what a validator sees
(`test_economic_drift_rejected_by_validator`). A leader **at or below** is
accepted. Two validators at 50% and 49% therefore converge on Tier 1 within one
rotation: the leader that read 49% is accepted by both
(`test_validators_straddling_tier_boundary_converge_without_liveness_deadlock`).
The validator with the lowest view is always accepted by every other validator in
the band, so rotation always reaches a leader that passes.

**What this costs, stated plainly.** A leader may now report a tier one step below
the truth when the true deficit is within 5 points above a threshold. That
direction favours LPs, so it is a (bounded) leader-honesty assumption on the
policyholder's side: it is possible only inside ±5 points of a threshold, never
skips a tier (thresholds are 25 points apart), and can be corrected by
`re_evaluate_revised_claim` only if the deficit later moves by more than the same
5 points. An upward manipulation is impossible, because a leader above any
validator is rejected.

**Backstop: `resolve_boundary_dispute`.** If a claim is still stuck, the
**policyholder** can call it. It settles at the *guaranteed lower bound*:
`conservative_tier = tier(certified_deficit − 5)`. Validators accept it when the
deficit is within the band and the tier is exactly that function of the leader's
deficit, so consensus no longer depends on which side of a threshold a validator
sits. Every validator that accepts it saw a tier at or above the paid tier. It
is policyholder-only because it can pay **less** than `evaluate_claim` (at 28% it
pays nothing, since 23% is Tier 0): it is a consented floor, not a default
(`test_boundary_dispute_resolves_at_guaranteed_lower_bound`).

**What validators compare.** The payload must carry exactly the four expected
keys with integer types (checked before any cast). Then the leader's payload and
the validator's own run must match as follows:

| Field | Comparison | Why |
|---|---|---|
| `source_count` | exact | it decides who may settle: a degraded two-source result is policyholder-only, and a leader must not be able to claim three answering sources when only two did (`test_validator_rejects_forged_source_count_payload`) |
| `median_deficit` | within **±5 points** | forecast-model updates or rounding between validator runs must not fail consensus and starve the policyholder (`test_validator_tolerates_deficit_noise_but_not_drift`) |
| `tier` | leader's tier ≤ validator's tier, and it must equal the tier of the leader's own deficit | the economic outcome can never exceed what any accepting validator sees. `resolve_boundary_dispute` instead requires `tier == tier(deficit − 5)` |
| outage (`-2`) | must be an outage for both | a leader cannot relabel an outage as a verdict |

The ±5 band is a judgment call, not a derived constant.

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
- **Data revisions are handled in two places.** Before evaluation, the 10-day
  stabilisation lag. After a payout is approved, the 24h cooldown allows
  `re_evaluate_revised_claim` (see Known limits, item 2). A quorum-failure retry
  can still read a revised value, because the first settlement is what is final.

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
2. **Data revisions.** The contract does not pin data versions. Three layers
   limit the exposure. (a) Evaluation opens 10 days after the last observed day,
   so preliminary ERA5 and NASA estimates have mostly settled. (b) A payout has a
   24-hour cooldown, and inside it `re_evaluate_revised_claim` re-reads all three
   sources and re-prices the payout up or down. (c) Noise is not a revision: the
   tier must change **and** the deficit must move by more than 5 points, at most
   3 times, so a payout cannot flap at a threshold. What is **not** covered:
   ECMWF may replace preliminary ERA5 with validated data weeks or months later.
   Once streaming begins the settlement is final, so a late revision cannot
   reopen it. A settled outcome therefore rests on the data as it stood by day
   10 plus the cooldown.
3. **Independent operators, correlated physics (see §5).** The three sources are
   three operators on three hostnames. ERA5 and NASA POWER both ingest satellite
   and station observations, and the NCEI gauge can be tens of kilometres from the
   plot.
4. **Admin ban binds one address, not a person.** An admin can still use a second
   wallet. Use a multisig for the admin, and watch registry changes (plots are
   public for 7 days before activation).
5. **Capital is locked for the grace period.** Expired-but-unevaluated policies
   keep their reserve until day 30 after term end, then release at once.
6. **Weather sources are public APIs, not a hardware oracle.** NASA POWER is a
   reanalysis product with a publication lag. The premium is a flat 8% with no
   actuarial pricing.
7. **Boundary resolution is asymmetric by design.** Validators accept a leader
   at or below their own tier, never above. That removes the straddle deadlock but
   lets a leader understate by one tier inside the ±5 band (see §2). The
   conservative backstop `resolve_boundary_dispute` can pay less than
   `evaluate_claim`, so only the policyholder may use it.
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
| 18 (round 8) | Comparing `median_deficit` exactly (round 7) made validators fail consensus on harmless numeric noise, such as a GFS model update or rounding, starving policyholders | `median_deficit` is compared within **±5 points** (`DEFICIT_NOISE_TOLERANCE`). `source_count` stays exact, so the settle permission cannot be forged. (The exact-`tier` rule that originally accompanied this was relaxed to a monotone rule in row 23.) Regression: `test_validator_tolerates_deficit_noise_but_not_drift` |
| 19 (steward) | Plot data was admin-typed text with no independent check | **`verify_plot_provenance`**: validators match the plot field by field to a manifest pinned to a git commit, and `cadastral_hash` must be the SHA-256 of the record. No plot is insurable until verified. Regression: `test_unverified_plot_provenance_cannot_be_insured`, `test_cadastral_hash_must_commit_to_the_staged_record` |
| 20 (steward) | Anyone could buy cover on someone else's farmland | Each plot binds one `eligible_policyholder`. `create_policy` reverts `ERR_INELIGIBLE_POLICYHOLDER`. Regression: `test_ineligible_policyholder_rejected_on_create_policy` |
| 21 (steward) | The same plot could be insured repeatedly | One live window per plot, and unresolved coverage capped at `max_plot_coverage`, `ERR_DUPLICATE_PLOT_COVERAGE`. Regression: `test_duplicate_overlapping_coverage_on_same_plot_rejected` |
| 22 (steward) | Two of three sources were two hostnames of one operator | NOAA NCEI GHCN-Daily gauge replaces the GFS leg. `_sources_independent` demands distinct hostnames and operator domains, at deploy and at every evaluation. Regression: `test_three_weather_sources_enforce_distinct_hostnames`, `test_contract_refuses_a_correlated_source_plan` |
| 23 (steward) | Validators straddling a tier threshold could rotate leaders until the claim expired unpaid | Monotone acceptance (leader at or below the validator's tier) plus the policyholder backstop `resolve_boundary_dispute`. Regression: `test_validators_straddling_tier_boundary_converge_without_liveness_deadlock`, `test_boundary_dispute_resolves_at_guaranteed_lower_bound` |
| 24 (steward) | Upstream observations revised after settlement had no remedy | 10-day maturity lag, and `re_evaluate_revised_claim` inside the 24h cooldown with the unpaid coverage held in reserve until `finalize_claim`. Regression: `test_revised_observations_during_cooldown_fairly_update_settlement_tier` and four more (see §0) |

**Deliberate deviation from the plain ERC-4626 formula.** A withdrawal pays
`min(shares × pool / total_shares, shares × (pool − reserved) / total_shares)`.
With only the first term, an LP could exit while a claim is still unrecognised
and leave the remaining LPs holding the loss. The cap means reserved capital never
leaves ahead of the claims it backs. `test_lp_share_fair_loss_distribution` proves
the leaver and the stayer end with identical value.

## 3. Verification & Testing

### 3a. Reproducing the 89 direct-mode contract tests

The suite runs the contract in the **direct VM execution mode** of
`genlayer-test` (gltest). The contract's Python executes in-process under the
GenVM SDK, with **mocked HTTP adapters** (`direct_vm.mock_web`) standing in for
the three weather sources and the cadastral manifest, and `direct_vm.warp` for the block clock. That isolates
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

**Commands** (the previous 67-test suite was verified from an empty cache and a
fresh virtualenv in 2m26s, almost all of it the one-time runner download. This
89-test suite was run on a warm cache, about 9 s.)

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
# lint + SDK validation of the contract (24 methods: 10 view, 14 write)
GENVM_VERSION=v0.6.0-rc5 genvm-lint check contracts/aether_claim.py

# frontend: types, helper tests (incl. exhaustive parity with the contract), build
cd frontend && npm install
npx tsc --noEmit
npm test                      # node --test tests/underwriting.test.ts
npm run build

# regenerate the Python <-> TypeScript parity vectors after a rule change
UPDATE_PARITY=1 python -m pytest tests/test_aether_claim.py -k Parity

# the manifest is generated and can be checked without a network
python3 registry/build_manifest.py --check
```

**Reproducing the live run** (what produced `deployments/studio-next.json`):

```bash
genlayer network set studio-next
genlayer account use aetherclaim-admin          # admin / deployer
genlayer deploy                                  # runs deploy/deploy_aether_claim.ts:
                                                 #   deploy, verify_plot_provenance x3,
                                                 #   provide_liquidity 0.5 GEN, readbacks

# the policy must be signed by the ELIGIBLE operator, not the admin.
# `genlayer deploy` runs every script in ./deploy of its cwd, so use a scratch dir:
mkdir -p /tmp/run/deploy && cp scripts/create_sample_policy.ts /tmp/run/deploy/
genlayer account use aetherclaim-farmer
(cd /tmp/run && PROJECT_ROOT=$OLDPWD genlayer deploy)    # create_policy 0.1 GEN on AND-OLIVE-01

genlayer call 0xA4c1eCC531d13486487827D40437FBF1E3CB8F2b list_plots
genlayer call 0xA4c1eCC531d13486487827D40437FBF1E3CB8F2b get_pool
genlayer call 0xA4c1eCC531d13486487827D40437FBF1E3CB8F2b get_policy --args P-1
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

## 5. Weather source independence

| Leg | Endpoint | Operator | What it is |
|---|---|---|---|
| ERA5 archive | `archive-api.open-meteo.com/v1/archive` | Open-Meteo (ECMWF ERA5) | gridded reanalysis |
| NASA POWER | `power.larc.nasa.gov/api/temporal/daily/point` | NASA Langley | satellite + model agroclimatology |
| NOAA NCEI GHCN-Daily | `www.ncei.noaa.gov/access/services/data/v1` (`daily-summaries`, `PRCP`) | NOAA | ground **rain-gauge** station, pinned per plot |

**What changed.** The previous second leg was the GFS forecast model served by
the same operator as ERA5, so two votes came from one party. It is replaced by
NOAA's GHCN-Daily gauge network, the only leg that is a physical measurement
rather than a model or reanalysis. The contract refuses to run unless the three
hostnames **and** the three operator domains are pairwise distinct.

**What this does not buy.**

- **Independent infrastructure, not independent physics.** ERA5 and NASA POWER
  both assimilate satellite and surface observations. They can share an upstream
  error that a hostname check cannot see.
- **Gauge representativeness.** A single GHCN station can be tens of kilometres
  from the plot. Convective rain can hit the plot and miss the gauge, or the
  reverse. The registry pins one station per plot, verified against the manifest,
  and a plot whose station is unrepresentative will see the gauge outvoted or
  outvoting.
- **Missing days abstain.** A gauge day with no row is a missing observation, not
  a dry day. The source drops out and the policy settles on two sources (the
  holder-only degraded path).
- **Publication lag.** GHCN-Daily and ERA5 are quality-controlled after the
  fact. The 10-day lag and the cooldown revision window reduce, but do not
  remove, that exposure (see §2, Known limits item 2).

Not integrated, and recommended before real money: a second independent gauge
per plot, and ECMWF's validated (non-preliminary) ERA5 product.

### Provenance trust model

`verify_plot_provenance` makes the registry **independently checkable**, not
**authoritative**.

- What it does prove: the on-chain plot matches, byte for byte in every
  settlement-relevant field, a manifest that is immutable (pinned to commit
  `3b39beb`), publicly fetchable by anyone, and whose cadastral hash is a
  recomputable SHA-256. Validators, not the admin, perform the check, and any
  address can trigger it.
- What it does not prove: that the coordinates, baselines or operator are true.
  The manifest is authored by the project. A real deployment needs the manifest
  to be issued by a land registry or a licensed agronomist, signed, and the
  operator binding to come from that authority. The contract is built so that
  swapping `REGISTRY_MANIFEST_URL` for such a source needs no other change.
- The admin cannot make a plot insurable by typing alone, and cannot alter a
  verified plot, because no function mutates a verified record.

## 6. Layout

```
contracts/aether_claim.py      Intelligent contract
registry/                      cadastral manifest + generator (the provenance source)
tests/                         pytest direct-mode suite (+ validator harness)
scripts/create_sample_policy.ts live create_policy from the eligible operator
deploy/deploy_aether_claim.ts  deploy + provenance verification + liquidity seed + readbacks + ABI export
deployments/                   ABI schema + deployment record (tx hashes, readbacks)
frontend/                      Next.js 14 + Tailwind dapp (+ tests/ helper + parity tests)
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
