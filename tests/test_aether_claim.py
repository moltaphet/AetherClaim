"""AetherClaim: lifecycle, adversarial, and audit regression suite (direct mode).

Run: python -m pytest tests/test_aether_claim.py -v
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from conftest import (
    ARID,
    ARID_APR,
    ARID_APR_PLOT,
    ARID_MAR,
    ARID_MAR_PLOT,
    ARID_PLOT,
    ATTO,
    CONTRACT,
    DAY,
    EXPIRED_AT,
    EXTRA_PLOTS,
    FLAT,
    GENESIS,
    GENESIS_MAX_COVERAGE,
    MATURITY,
    MATURITY_LAG_DAYS,
    NORMAL,
    OBS_END,
    OBS_START,
    PLOT,
    POLICY_DAYS,
    SEASONAL,
    SEASONAL_PLOT,
    T0,
    TEST_MAX_COVERAGE,
    TEST_PLOT,
    TEST_STATION,
    URL_NASA,
    URL_NCEI,
    URL_OM,
    TIER1,
    TIER2,
    TIER3,
    buy,
    cadastral_digest,
    genesis_records,
    manifest_body,
    manifest_record,
    mock_manifest,
    mock_sources,
    provide,
    stage,
    uniform,
    warp_to,
    _nasa_body,
    _ncei_body,
    _om_body,
)

CV = "CV-ALMOND-01"


def _build_env(direct_vm, direct_deploy, alice, bob, charlie, verify):
    """Deploy, stage the test plots, activate them and (optionally) verify every
    plot against the independent manifest. Test plots are staged 9 days before T0
    and activated 8 days later, clear of the 7-day staging timelock. The
    genesis registry is bound to `charlie` (the farmer)."""
    warp_to(direct_vm, T0 - timedelta(days=9))
    direct_vm.sender = alice  # deployer == admin
    farmer_hex = "0x" + bytes(charlie).hex()
    c = direct_deploy(CONTRACT, 500 * ATTO, farmer_hex)
    test_plots = [
        (TEST_PLOT, FLAT),
        (ARID_PLOT, ARID),
        (SEASONAL_PLOT, SEASONAL),
        (ARID_MAR_PLOT, ARID_MAR),
        (ARID_APR_PLOT, ARID_APR),
    ] + [(p, FLAT) for p in EXTRA_PLOTS]
    records = genesis_records(farmer_hex)
    for pid, monthly in test_plots:
        records[pid] = stage(c, pid, monthly, farmer_hex)
    warp_to(direct_vm, T0 - timedelta(days=1))
    for pid, _ in test_plots:
        c.activate_plot(pid)
    warp_to(direct_vm, T0)
    if verify:
        mock_manifest(direct_vm, records)
        for pid in records:
            assert c.verify_plot_provenance(pid)["verified"] is True, pid
    provide(c, direct_vm, bob, 100 * ATTO)

    class E:
        pass

    e = E()
    e.vm, e.c = direct_vm, c
    e.admin, e.lp, e.farmer = alice, bob, charlie
    e.farmer_hex = farmer_hex
    e.records = records
    return e


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    return _build_env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie, True)


@pytest.fixture
def raw_env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """Same registry, but NO plot has been verified yet."""
    return _build_env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie, False)


def claim_with(e, mm, pid=None):
    pid = pid or buy(e.c, e.vm, e.farmer)
    warp_to(e.vm, MATURITY + timedelta(hours=3))
    uniform(e.vm, mm)
    e.vm.sender = e.farmer
    return pid, e.c.evaluate_claim(pid)


def _hex(env, who):
    env.vm.sender = who
    return env.c.whoami()


# ----------------------------------------------------------- normal lifecycle
class TestLifecycle:
    def test_genesis_registry(self, env):
        ids = {p["plot_id"] for p in env.c.list_plots()}
        assert {"CV-ALMOND-01", "IA-CORN-01", "AND-OLIVE-01"} <= ids
        cv = env.c.get_plot(CV)
        assert cv["monthly_baselines"][0] == 50 and len(cv["monthly_baselines"]) == 12
        assert (cv["crop_cycle_start_month"], cv["crop_cycle_end_month"]) == (11, 3)  # Nov-Mar
        assert cv["crop_cycle"] == "Nov-Mar"
        assert env.c.get_plot("AND-OLIVE-01")["crop_cycle_start_month"] == 10  # wraps year end

    def test_stage_then_activate_after_delay(self, env):
        env.vm.sender = env.admin
        stage(env.c, "NEW-01", FLAT, env.farmer_hex, c_start=3, c_end=10)
        assert env.c.get_plot("NEW-01")["active"] is False
        warp_to(env.vm, T0 + timedelta(days=1, minutes=1))  # old 24h delay is no longer enough
        with env.vm.expect_revert("TIMELOCK"):
            env.c.activate_plot("NEW-01")
        warp_to(env.vm, T0 + timedelta(days=7, minutes=1))
        env.c.activate_plot("NEW-01")
        assert env.c.get_plot("NEW-01")["active"] is True

    def test_policy_issuance_and_accounting(self, env):
        pid = buy(env.c, env.vm, env.farmer, 10 * ATTO)
        pol = env.c.get_policy(pid)
        assert pol["premium_paid"] == str(10 * ATTO * 800 // 10000)
        assert pol["status"] == "ACTIVE"
        pool = env.c.get_pool()
        assert int(pool["total_liabilities"]) == 10 * ATTO
        assert int(pool["pool_balance"]) == 100 * ATTO + int(pol["premium_paid"])

    @pytest.mark.parametrize(
        "mm,tier,label,bps",
        [
            (TIER1, 1, "MODERATE_DROUGHT", 3000),
            (TIER2, 2, "SEVERE_DROUGHT", 7000),
            (TIER3, 3, "CATASTROPHIC_DROUGHT", 10000),
        ],
    )
    def test_tiered_payouts(self, env, mm, tier, label, bps):
        pid, claim = claim_with(env, mm)
        assert claim["payout_tier"] == tier and claim["outcome"] == label
        assert claim["source_count"] == 3
        assert int(claim["approved_payout"]) == 10 * ATTO * bps // 10000
        pol = env.c.get_policy(pid)
        assert pol["claimed"] is True and pol["status"] == "SETTLED_PAYOUT"
        assert int(env.c.get_pool()["committed_payouts"]) == int(claim["approved_payout"])

    def test_normal_rainfall_settles_final_and_releases_reserve(self, env):
        pid, claim = claim_with(env, NORMAL)
        assert claim["outcome"] == "NORMAL" and claim["approved_payout"] == "0"
        pol = env.c.get_policy(pid)
        assert pol["status"] == "SETTLED_NO_PAYOUT" and pol["claimed"] is False
        assert int(env.c.get_pool()["total_liabilities"]) == 0  # reserve released
        env.vm.sender = env.farmer
        with env.vm.expect_revert("not claimable"):
            env.c.evaluate_claim(pid)  # no second look after a settled outcome

    def test_linear_streaming_release(self, env):
        pid, claim = claim_with(env, TIER3)
        payout = int(claim["approved_payout"])
        cool = claim["cooldown_until"]
        warp_to(env.vm, datetime.fromtimestamp(cool + int(1.5 * DAY), tz=timezone.utc))
        released = env.c.release_payout(pid)
        assert released == payout // 2
        assert int(env.c.get_claim(pid)["vested_amount"]) == payout // 2
        warp_to(env.vm, datetime.fromtimestamp(cool + 3 * DAY + 5, tz=timezone.utc))
        released2 = env.c.release_payout(pid)
        assert released + released2 == payout
        assert int(env.c.get_pool()["committed_payouts"]) == 0
        with env.vm.expect_revert("nothing vested"):
            env.c.release_payout(pid)

    def test_expire_releases_reserve_after_grace(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, T0 + timedelta(days=POLICY_DAYS + 15))  # still inside the grace
        with env.vm.expect_revert("grace period"):
            env.c.expire_policy(pid)
        warp_to(env.vm, EXPIRED_AT)
        env.c.expire_policy(pid)
        assert int(env.c.get_pool()["total_liabilities"]) == 0

    def test_observation_window_is_fixed_to_final_28_days(self, env):
        """Windows are the last 28 days of the term, recorded at purchase, and a
        later evaluation cannot move them."""
        pid = buy(env.c, env.vm, env.farmer)
        pol = env.c.get_policy(pid)
        assert pol["observation_end"] == int(OBS_END.timestamp())
        assert pol["observation_start"] == int(OBS_START.timestamp())
        assert pol["matures_at"] == int(MATURITY.timestamp())
        warp_to(env.vm, MATURITY + timedelta(days=3))
        uniform(env.vm, TIER3)  # mocked for the policy's own window, not for today's
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3

    def test_grace_and_aggregation_config(self, env):
        cfg = env.c.get_config()
        assert cfg["expiry_grace_days"] == 30
        assert cfg["quorum_min_sources"] == 2
        assert cfg["min_insurable_baseline_mm"] == 10
        assert cfg["maturity_lag_days"] == MATURITY_LAG_DAYS == 10
        assert "data_lag_days" not in cfg
        assert "retry_hold_days" not in cfg  # no hold: grace is the only retry window


# ----------------------------------------------------------------- adversarial
class TestAdversarialProtection:
    def test_arbitrary_plot_rejected(self, env):
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("authenticated registry"):
            env.c.create_policy("MY-OWN-FARM-0,0", 10**18)

    def test_staged_plot_not_insurable_until_active(self, env):
        env.vm.sender = env.admin
        stage(env.c, "NEW-02", FLAT, env.farmer_hex)
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("not active"):
            env.c.create_policy("NEW-02", 10**18)

    def test_only_admin_registers_plots(self, env):
        env.vm.sender = env.farmer
        with env.vm.expect_revert("only admin"):
            env.c.stage_plot("EVIL", "x", "x", 0, 0, FLAT, "c" * 64, 1, 12, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)

    def test_single_station_fault_cannot_trigger_payout(self, env):
        """One station reports 0 mm (tier 3) while two report normal rain. The
        median is tier 0, so the isolated reading cannot pay anything."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (0.0, 0.0), ncei=(NORMAL, NORMAL), nasa=(NORMAL, NORMAL))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "NORMAL" and claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"
        warp_to(env.vm, MATURITY + timedelta(days=2))
        mock_sources(env.vm, (TIER3, TIER3), ncei=(TIER3, TIER3), nasa=(TIER3, TIER3))
        with env.vm.expect_revert("not claimable"):
            env.c.evaluate_claim(pid)  # settled is final: no second look

    def test_source_disagreement_settles_at_conservative_lower_tier(self, env):
        """Audit PoC. Open-Meteo (ERA5) reads Tier 3, NASA POWER reads Tier 1, and
        the NOAA NCEI leg is down. Two sources answer, so the lower tier settles: Tier 1
        (30%), deterministically. It does not settle at 0, and it does not pay 100%."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=(TIER1, TIER1))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["outcome"] == "MODERATE_DROUGHT"
        assert claim["source_count"] == 2
        assert int(claim["approved_payout"]) == 3 * ATTO  # 30% of 10 GEN
        assert claim["reasoning"].startswith("2 sources answered: conservative floor tier 1")
        pol = env.c.get_policy(pid)
        assert pol["status"] == "SETTLED_PAYOUT" and pol["claimed"] is True
        # Determinism: a validator that re-reads the same sources agrees.
        assert env.vm.run_validator() is True

    def test_third_party_cannot_force_lower_tier_settlement_on_outage(self, env):
        """Audit PoC (commit 4e3a10b). NOAA NCEI is down; ERA5 reads Tier 1 and NASA
        Tier 3. A third party calls evaluate_claim. It must NOT lock the insured
        into min = Tier 1. The call is a QUORUM_FAILURE that writes no state, and
        when NCEI returns the insured still settles at the Tier 3 median."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER1, TIER1), ncei=None, nasa=(TIER3, TIER3))
        env.vm.sender = env.lp  # a third party, not the policyholder
        res = env.c.evaluate_claim(pid)
        assert res["outcome"] == "QUORUM_FAILURE" and res["source_count"] == 2
        assert env.c.get_policy(pid)["status"] == "ACTIVE"
        assert env.c.get_claim(pid) == {}  # nothing written: no lock, no used retry slot
        # Repeated griefing never consumes the policyholder's retry.
        env.vm.sender = env.admin
        assert env.c.evaluate_claim(pid)["outcome"] == "QUORUM_FAILURE"
        # NCEI recovers. All three answer: ERA5 Tier 1, NCEI Tier 3, NASA Tier 3.
        mock_sources(env.vm, (TIER1, TIER1), ncei=(TIER3, TIER3), nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)  # no "too recently": nothing was recorded
        assert claim["payout_tier"] == 3 and int(claim["approved_payout"]) == 10 * ATTO

    def test_three_sources_settle_permissionlessly_at_median(self, env):
        """With all three answering, any caller may settle, at the median."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER1, TIER1), ncei=(TIER3, TIER3), nasa=(TIER3, TIER3))
        env.vm.sender = env.lp  # third party
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and claim["source_count"] == 3
        pol = env.c.get_policy(pid)
        assert pol["status"] == "SETTLED_PAYOUT" and pol["claimed"] is True
        assert pol["policyholder"] == _hex(env, env.farmer)  # payout still belongs to the insured

    def test_policyholder_can_voluntarily_accept_degraded_two_source_settlement(self, env):
        """The insured may choose the conservative lower tier with two sources."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER1, TIER1), ncei=None, nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["source_count"] == 2
        assert int(claim["approved_payout"]) == 3 * ATTO
        assert env.c.get_policy(pid)["status"] == "SETTLED_PAYOUT"

    def test_third_party_outage_attempt_does_not_block_policyholder_retry(self, env):
        """With fewer than two sources, a third party's call also writes nothing."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)
        env.vm.sender = env.lp
        assert env.c.evaluate_claim(pid)["outcome"] == "QUORUM_FAILURE"
        assert env.c.get_claim(pid) == {}
        uniform(env.vm, TIER3)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3

    def test_three_source_split_settles_at_median(self, env):
        """All three answer: Tier 3, Tier 1, Tier 1. The median is Tier 1."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=(TIER1, TIER1), nasa=(TIER1, TIER1))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["source_count"] == 3
        assert claim["reasoning"].startswith("3 sources answered: median tier 1")

    def test_single_low_outlier_is_outvoted(self, env):
        """Two sources read Tier 3, one reads normal rain. The median is Tier 3, so
        one faulty low reading cannot wipe out a drought that two sources see.
        The three sources are on three operators, so each vote is independent."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=(TIER3, TIER3), nasa=(NORMAL, NORMAL))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and int(claim["approved_payout"]) == 10 * ATTO

    def test_two_source_split_settles_at_lower_reading_even_if_zero(self, env):
        """Documented limitation. With only two sources answering, the lower reading
        governs, so one normal-rain reading can settle a drought at zero. The
        three-source median does not have this weakness."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=(NORMAL, NORMAL), nasa=None)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 0 and claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"

    def test_too_few_sources_is_quorum_failure_and_retryable(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "QUORUM_FAILURE" and claim["source_count"] == 1
        assert env.c.get_policy(pid)["status"] == "ACTIVE"  # still open for a retry

    def test_two_of_three_sources_can_settle(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and claim["source_count"] == 2

    def test_single_window_anomaly_pays_nothing(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, NORMAL))
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["outcome"] == "NORMAL"

    def test_sustained_tier_is_weakest_window(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER1))
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 1

    def test_economic_drift_rejected_by_validator(self, env):
        """Leader proposes Tier 2 at 50% deficit; a validator seeing 49% lands in
        Tier 1. A leader ABOVE the validator's own tier is rejected, so the payout
        is never pushed past what a validator sees. (The reverse direction is
        accepted: see the boundary-convergence test.)"""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, 7.0)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 2
        uniform(env.vm, 7.1)
        assert env.vm.run_validator() is False

    def test_validator_tolerates_deficit_noise_but_not_drift(self, env):
        """median_deficit is telemetry and may differ by up to 5 points between
        validator runs. 5.0 mm is a 64% deficit and 5.4 mm is 61%: same tier, in
        tolerance, accepted. 6.5 mm is 53%: same tier, but 11 points away, so it is
        rejected as drift rather than noise."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        assert env.vm.run_validator() is True  # identical readings
        uniform(env.vm, 5.4)  # 3 points
        assert env.vm.run_validator() is True
        uniform(env.vm, 6.5)  # 11 points, still tier 2
        assert env.vm.run_validator() is False

    def test_validator_rejects_leader_error(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        assert env.vm.run_validator(leader_error=Exception("boom")) is False

    def test_validator_rejects_fabricated_tier3(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, NORMAL)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        fake = {"tier": 3, "median_deficit": 99, "source_count": 3, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=fake) is False

    def test_cooldown_blocks_instant_drain(self, env):
        pid, _ = claim_with(env, TIER3)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("TIMELOCK"):
            env.c.release_payout(pid)

    def test_claim_cannot_be_evaluated_before_observation_maturity(self, env):
        """No daily cherry-picking: a verdict cannot be taken before the windows
        have matured, even when the mocked weather would pay."""
        pid = buy(env.c, env.vm, env.farmer)
        uniform(env.vm, TIER3)
        env.vm.sender = env.farmer
        for early in (T0 + timedelta(days=30), T0 + timedelta(days=POLICY_DAYS), MATURITY - timedelta(minutes=1)):
            warp_to(env.vm, early)
            with env.vm.expect_revert("not matured"):
                env.c.evaluate_claim(pid)
        warp_to(env.vm, MATURITY)
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3

    def test_evaluation_closes_after_grace(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        uniform(env.vm, TIER3)
        warp_to(env.vm, EXPIRED_AT)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("expired"):
            env.c.evaluate_claim(pid)

    def test_availability_cannot_raise_payout(self, env):
        """Exhaustive sweep. Every combination of tiers at the three sources, and
        every pattern of which sources answer. For each, the contract's aggregate
        must equal the independent oracle, and the oracle must never rise above the
        all-three median when a source drops out. Sources drop out at random, so
        this rules out gaming availability."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, NORMAL)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)  # captures the validator for this policy's fixed windows

        mm_for = {0: NORMAL, 1: TIER1, 2: TIER2, 3: TIER3}
        deficit_for = {0: 0, 1: 28, 2: 64, 3: 85}  # sustained deficit for each tier mm
        pair = lambda mm: (mm, mm) if mm is not None else None  # noqa: E731

        def oracle(tiers, mask):
            answered = [t for t, m in zip(tiers, mask) if m]
            if len(answered) < 2:
                return -2, 0
            k = 1 if len(answered) == 3 else 0
            tier = sorted(answered)[k]
            med = sorted(deficit_for[t] for t in answered)[k]
            return tier, med

        checked = 0
        for tiers in product(range(4), repeat=3):
            full_tier, _ = oracle(tiers, (True, True, True))
            for mask in product([True, False], repeat=3):
                mms = [mm_for[t] if m else None for t, m in zip(tiers, mask)]
                mock_sources(env.vm, pair(mms[0]), ncei=pair(mms[1]), nasa=pair(mms[2]))
                tier, med = oracle(tiers, mask)
                assert tier <= full_tier or tier == -2, (tiers, mask)  # never above the median
                count = sum(mask)
                payload = {"tier": tier, "median_deficit": med, "source_count": count, "reasoning": "x"}
                assert env.vm.run_validator(leader_result=payload) is True, (tiers, mask)
                if 0 <= tier <= 2:  # the contract compares tiers exactly
                    higher = {**payload, "tier": tier + 1, "median_deficit": deficit_for[tier + 1]}
                    assert env.vm.run_validator(leader_result=higher) is False, (tiers, mask)
                checked += 1
        assert checked == 4**3 * 2**3

    def test_quorum_failure_retry_then_settlement_is_final(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)  # one source only
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["outcome"] == "QUORUM_FAILURE"
        with env.vm.expect_revert("too recently"):
            env.c.evaluate_claim(pid)
        warp_to(env.vm, MATURITY + timedelta(days=1, hours=3))  # sources recovered
        uniform(env.vm, TIER3)
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3
        with env.vm.expect_revert("not claimable"):
            env.c.evaluate_claim(pid)

    def test_quorum_failure_retries_then_releases_at_grace_end(self, env):
        """Retries are allowed until grace ends. Once it has, expiry releases the
        reserve immediately. There is no separate hold after the last attempt."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, T0 + timedelta(days=POLICY_DAYS + 29, hours=3))  # late in grace
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)  # outage
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["outcome"] == "QUORUM_FAILURE"
        warp_to(env.vm, EXPIRED_AT)  # grace over; the last try was hours ago
        env.c.expire_policy(pid)
        assert env.c.get_policy(pid)["status"] == "EXPIRED"
        assert int(env.c.get_pool()["total_liabilities"]) == 0

    def test_epoch_exposure_cap(self, env):
        env.vm.sender = env.admin
        env.c.set_max_epoch_exposure(15 * ATTO)
        buy(env.c, env.vm, env.farmer, 10 * ATTO)
        env.vm.sender = env.farmer
        env.vm.value = 6 * ATTO * 800 // 10000
        with env.vm.expect_revert("epoch exposure cap"):
            env.c.create_policy(EXTRA_PLOTS[0], 6 * ATTO)

    def test_solvency_cap(self, env):
        env.vm.sender = env.admin
        env.c.set_max_epoch_exposure(10_000 * ATTO)
        for plot in [PLOT] + EXTRA_PLOTS[:3]:  # 4 x 25 = 100 of ~108 pool
            buy(env.c, env.vm, env.farmer, 25 * ATTO, plot=plot)
        env.vm.sender = env.farmer
        env.vm.value = 25 * ATTO * 800 // 10000
        with env.vm.expect_revert("solvency"):
            env.c.create_policy(EXTRA_PLOTS[3], 25 * ATTO)

    def test_concentration_cap(self, env):
        env.vm.sender = env.admin
        env.c.set_max_epoch_exposure(10_000 * ATTO)
        env.vm.sender = env.farmer
        env.vm.value = 60 * ATTO * 800 // 10000
        with env.vm.expect_revert("concentration"):
            env.c.create_policy(PLOT, 60 * ATTO)

    def test_wrong_premium_rejected(self, env):
        env.vm.sender = env.farmer
        env.vm.value = 1
        with env.vm.expect_revert("premium must be exactly"):
            env.c.create_policy(PLOT, 10 * ATTO)

    def test_lp_cannot_withdraw_reserved_capital(self, env):
        """A partial exit pays only the unreserved slice; reserved capital stays."""
        buy(env.c, env.vm, env.farmer, 20 * ATTO)  # pool 101.6, reserve 20
        env.vm.sender = env.lp  # sole LP, 100 shares
        out = env.c.withdraw_liquidity(50 * ATTO)
        assert out == 408 * ATTO // 10  # min(50.8 pro rata, 40.8 of unreserved)
        pool = env.c.get_pool()
        assert int(pool["pool_balance"]) >= int(pool["total_liabilities"]) == 20 * ATTO

    def test_last_lp_cannot_orphan_reserved_capital(self, env):
        """Burning 100% of shares while liabilities are live would strand the
        reserve with no shareholder. The last LP may only leave once policies
        have settled."""
        buy(env.c, env.vm, env.farmer, 20 * ATTO)
        env.vm.sender = env.lp  # sole LP, 100 shares
        with env.vm.expect_revert("last LP cannot exit"):
            env.c.withdraw_liquidity(100 * ATTO)
        assert env.c.get_lp(_hex(env, env.lp))["shares"] == str(100 * ATTO)  # nothing moved
        env.c.withdraw_liquidity(50 * ATTO)  # partial exit still allowed

        warp_to(env.vm, EXPIRED_AT)
        env.c.expire_policy("P-1")  # liabilities clear
        env.c.withdraw_liquidity(50 * ATTO)  # now the final exit is allowed
        pool = env.c.get_pool()
        assert pool["total_lp_shares"] == "0" and pool["pool_balance"] == "0"

    # Round 4: worthless-policy guard
    @pytest.mark.parametrize("plot", [ARID_PLOT, ARID_MAR_PLOT, ARID_APR_PLOT])
    def test_arid_term_reverts_at_purchase(self, env, plot):
        """An arid term (any final window under the 10 mm floor) must not be sold:
        it could only ever settle at 0%. Both windows are checked, so an arid March
        or an arid April each reverts on its own."""
        warp_to(env.vm, datetime(2026, 1, 10, tzinfo=timezone.utc))  # term Jan 10 - Apr 10
        env.vm.sender = env.farmer
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("Term uninsurable: observation window falls in arid month"):
            env.c.create_policy(plot, 10 * ATTO)

    def test_arid_control_plot_still_insurable(self, env):
        """Control: the same term on the 30 mm/month plot is accepted."""
        warp_to(env.vm, datetime(2026, 1, 10, tzinfo=timezone.utc))
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot=TEST_PLOT).startswith("P-")


# ----------------------------------------------------------- audit regressions
class TestAuditRegressions:
    # Issue #1: seasonal baselines and crop cycle
    def test_seasonal_summer_exploit_prevented(self, env):
        """Olive groves are dormant and dry Apr-Sep. A June policy would pay out
        every year for ordinary seasonal dryness; it must be refused."""
        warp_to(env.vm, datetime(2026, 6, 1, tzinfo=timezone.utc))
        env.vm.sender = env.farmer
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("off-season"):
            env.c.create_policy("AND-OLIVE-01", 10 * ATTO)

    def test_term_must_fit_inside_cycle_not_just_start(self, env):
        """Corn cycle is Apr-Oct: buying on 2026-03-01 starts off-season."""
        env.vm.sender = env.farmer
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("off-season"):
            env.c.create_policy("IA-CORN-01", 10 * ATTO)
        warp_to(env.vm, datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="IA-CORN-01").startswith("P-")

    def test_wrapping_cycle_accepts_winter_term(self, env):
        warp_to(env.vm, datetime(2026, 10, 15, tzinfo=timezone.utc))  # Oct..Jan, in Oct-Mar
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="AND-OLIVE-01").startswith("P-")

    def test_per_month_baselines_drive_the_deficit(self, env):
        """Term 2026-01-10 to 2026-04-10. Final windows: Mar 14-27 (baseline 14 mm,
        March 30 mm) and Mar 28-Apr 9 (baseline 28 mm, April 60 mm). Observed
        5 mm and 10 mm are each 64% and 64% below their own baselines: Tier 2.
        Judged against the start-month baseline instead it would be Tier 1."""
        warp_to(env.vm, datetime(2026, 1, 10, tzinfo=timezone.utc))
        pid = buy(env.c, env.vm, env.farmer, 10 * ATTO, plot=SEASONAL_PLOT)
        obs_end = datetime(2026, 4, 9, tzinfo=timezone.utc)
        warp_to(env.vm, datetime(2026, 4, 20, 3, tzinfo=timezone.utc))  # last observed day + 11 days
        mock_sources(env.vm, (5.0, 10.0), obs_end=obs_end)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 2

    def test_stage_plot_validates_cycle_and_baselines(self, env):
        env.vm.sender = env.admin
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B1", "x", "x", 0, 0, FLAT, "c" * 64, 0, 6, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B2", "x", "x", 0, 0, FLAT, "c" * 64, 3, 13, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B3", "x", "x", 0, 0, FLAT[:11], "c" * 64, 3, 10, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B4", "x", "x", 0, 0, [-1] + FLAT[1:], "c" * 64, 3, 10, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)

    # Round 4: Central Valley winter programme
    def test_central_valley_summer_term_rejected(self, env):
        warp_to(env.vm, datetime(2026, 6, 1, tzinfo=timezone.utc))
        env.vm.sender = env.farmer
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("off-season"):
            env.c.create_policy(CV, 10 * ATTO)

    def test_central_valley_purchase_calendar(self, env):
        """Purchases start Nov 1 to Dec 31. A term starting Dec 31 ends Mar 31, the
        last in-season day. A term starting Jan 1 reaches April, which is off-season."""
        warp_to(env.vm, datetime(2026, 12, 31, tzinfo=timezone.utc))
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot=CV).startswith("P-")
        warp_to(env.vm, datetime(2027, 1, 1, tzinfo=timezone.utc))
        env.vm.sender = env.farmer
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("off-season"):
            env.c.create_policy(CV, 10 * ATTO)

    def test_central_valley_winter_policy_insurable_and_settles(self, env):
        """Term 2026-11-01 to 2027-01-30. Both final windows fall in January,
        baseline 50 mm/month, 23.3 mm per window. Observed 15 mm in each is a 35%
        deficit: Tier 1, a 30% payout."""
        warp_to(env.vm, datetime(2026, 11, 1, tzinfo=timezone.utc))
        pid = buy(env.c, env.vm, env.farmer, 10 * ATTO, plot=CV)
        pol = env.c.get_policy(pid)
        assert pol["observation_end"] == int(datetime(2027, 1, 29, tzinfo=timezone.utc).timestamp())
        assert pol["observation_start"] == int(datetime(2027, 1, 2, tzinfo=timezone.utc).timestamp())
        warp_to(env.vm, datetime(2027, 2, 9, 3, tzinfo=timezone.utc))  # last observed day + 11 days
        mock_sources(env.vm, (15.0, 15.0), obs_end=datetime(2027, 1, 29, tzinfo=timezone.utc))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["outcome"] == "MODERATE_DROUGHT"
        assert claim["median_deficit"] == 35
        assert int(claim["approved_payout"]) == 3 * ATTO  # 30% of 10 GEN
        assert env.c.get_policy(pid)["status"] == "SETTLED_PAYOUT"

    # Round 2/3 carried forward: admin self-dealing, vault shares, payloads
    def test_admin_self_insurance_prevented(self, env):
        env.vm.sender = env.admin
        stage(env.c, "ADM-01", FLAT, env.farmer_hex)
        warp_to(env.vm, T0 + timedelta(days=7, minutes=1))
        env.c.activate_plot("ADM-01")
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("Admin cannot hold insurance policies"):
            env.c.create_policy("ADM-01", 10 * ATTO)
        with env.vm.expect_revert("Admin cannot hold insurance policies"):
            env.c.create_policy(PLOT, 10 * ATTO)

    def test_staging_delay_is_seven_days(self, env):
        assert env.c.get_config()["staging_delay_seconds"] == 7 * DAY

    def test_exposure_cap_bounds(self, env):
        env.vm.sender = env.admin
        with env.vm.expect_revert("out of bounds"):
            env.c.set_max_epoch_exposure(0)
        with env.vm.expect_revert("out of bounds"):
            env.c.set_max_epoch_exposure(10**30)
        env.c.set_max_epoch_exposure(50 * ATTO)

    def test_lp_share_fair_loss_distribution(self, env):
        """Two equal LPs. One tries to leave just before a claim is recognised;
        the remaining LP must not be left holding the whole loss."""
        provide(env.c, env.vm, env.admin, 100 * ATTO)  # second LP, equal shares
        buy(env.c, env.vm, env.farmer, 20 * ATTO)  # 1.6 premium, 20 reserved
        env.vm.sender = env.lp
        early_exit = env.c.withdraw_liquidity(100 * ATTO)  # bob burns all of his shares
        assert early_exit == (201_600_000_000_000_000_000 - 20 * ATTO) // 2

        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER3)  # catastrophic: full 20 GEN payout
        env.vm.sender = env.farmer
        assert int(env.c.evaluate_claim("P-1")["approved_payout"]) == 20 * ATTO

        stayer = int(env.c.get_lp(_hex(env, env.admin))["value"])
        assert stayer == early_exit

    def test_lp_loss_is_proportional_when_all_stay(self, env):
        provide(env.c, env.vm, env.admin, 300 * ATTO)  # 3:1 split
        pid = buy(env.c, env.vm, env.farmer, 20 * ATTO)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER3)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        bob = int(env.c.get_lp(_hex(env, env.lp))["value"])
        alice = int(env.c.get_lp(_hex(env, env.admin))["value"])
        assert abs(alice - 3 * bob) <= 3  # rounding dust only
        assert abs(bob + alice - int(env.c.get_pool()["pool_balance"])) <= 2

    def test_lp_earns_accumulated_premiums(self, env):
        buy(env.c, env.vm, env.farmer, 10 * ATTO)  # 0.8 GEN premium into the pool
        warp_to(env.vm, EXPIRED_AT)
        env.vm.sender = env.farmer
        env.c.expire_policy("P-1")  # unclaimed: reserve released, premium kept
        assert env.c.get_pool()["share_price_atto"] == str(1008 * 10**15)

        provide(env.c, env.vm, env.admin, 100800 * 10**15)  # buys in at 1.008
        assert env.c.get_lp(_hex(env, env.admin))["shares"] == str(100 * ATTO)

        env.vm.sender = env.lp
        out = env.c.withdraw_liquidity(100 * ATTO)
        assert out == 100800 * 10**15  # principal + accrued premium

    def test_dust_deposit_rejected(self, env):
        env.vm.sender = env.farmer
        env.vm.value = 1000
        with env.vm.expect_revert("below minimum"):
            env.c.provide_liquidity()

    def test_cannot_burn_more_shares_than_owned(self, env):
        env.vm.sender = env.farmer
        with env.vm.expect_revert("exceeds your shares"):
            env.c.withdraw_liquidity(1)

    def test_validator_rejects_malformed_leader_payload(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)

        good = {"tier": 2, "median_deficit": 64, "source_count": 3, "reasoning": "ok"}
        assert env.vm.run_validator(leader_result=good) is True

        bad = {
            "negative deficit": {**good, "median_deficit": -5},
            "deficit over 100": {**good, "median_deficit": 400},
            "tier out of range": {**good, "tier": 9},
            "tier below range": {**good, "tier": -3},
            "tier not int": {**good, "tier": "2"},
            "bool tier": {**good, "tier": True},
            "float deficit": {**good, "median_deficit": 64.5},
            "tier/deficit mismatch": {**good, "tier": 3, "median_deficit": 10},
            "settled below quorum": {**good, "source_count": 1},
            "quorum failure with 3 sources": {"tier": -2, "median_deficit": 0, "source_count": 3, "reasoning": "x"},
            "retired split code -1": {"tier": -1, "median_deficit": 50, "source_count": 3, "reasoning": "x"},
            "too many sources": {**good, "source_count": 99},
            "oversized reasoning": {**good, "reasoning": "A" * 5000},
            "control chars": {**good, "reasoning": "ok\x00\x1b[31m"},
            "reasoning not str": {**good, "reasoning": {"x": 1}},
            "missing key": {k: v for k, v in good.items() if k != "reasoning"},
            "not a dict": [2, 64, 3, "ok"],
        }
        for name, payload in bad.items():
            assert env.vm.run_validator(leader_result=payload) is False, name

    def test_settled_tier_is_consensus_checked(self, env):
        """Three sources answer with ERA5 at Tier 3 and the others at Tier 1. The
        median is Tier 1. A leader cannot claim a different tier, nor relabel the
        split as an outage, and the honest median passes."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=(TIER1, TIER1), nasa=(TIER1, TIER1))
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        assert env.vm.run_validator(leader_result={"tier": 1, "median_deficit": 28, "source_count": 3, "reasoning": "x"}) is True
        assert env.vm.run_validator(leader_result={"tier": 3, "median_deficit": 85, "source_count": 3, "reasoning": "x"}) is False
        assert env.vm.run_validator(leader_result={"tier": 0, "median_deficit": 0, "source_count": 3, "reasoning": "x"}) is False
        relabelled = {"tier": -2, "median_deficit": 0, "source_count": 1, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=relabelled) is False

    def test_validator_rejects_forged_source_count_payload(self, env):
        """Audit PoC (commit ab52505). Only two sources answer (tier 1). A leader
        forges source_count 3 with the agreeing tier to bypass the policyholder-only
        degraded-settlement guard. Tier equality alone accepted it. The validator
        must now compare source_count exactly as well."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER1, TIER1), ncei=None, nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["source_count"] == 2
        honest = {"tier": 1, "median_deficit": 28, "source_count": 2, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=honest) is True
        forged = {**honest, "source_count": 3}  # tier matches, count is a lie
        assert env.vm.run_validator(leader_result=forged) is False
        # median_deficit: 5 points of noise is tolerated, 6 is not. Tier and
        # source_count carry the guarantees, so the forgery stays rejected above.
        assert env.vm.run_validator(leader_result={**honest, "median_deficit": 33}) is True
        assert env.vm.run_validator(leader_result={**honest, "median_deficit": 34}) is False
        assert env.vm.run_validator(leader_result={**honest, "median_deficit": 85}) is False
        assert env.vm.run_validator(leader_result={**honest, "extra": 1}) is False

    def test_stored_reasoning_is_canonical_not_leader_text(self, env):
        _, claim = claim_with(env, TIER2)
        assert claim["reasoning"] == "3 sources answered: median tier 2 (SEVERE_DROUGHT), median sustained deficit 64%"


# ------------------------------------------------- steward round: underwriting
CONTRACT_MODULE = "_contract_aether_claim"


def _contract_module():
    return sys.modules[CONTRACT_MODULE]


def _expect_buy_revert(env, who, plot, msg, coverage=10 * ATTO):
    env.vm.sender = who
    env.vm.value = coverage * 800 // 10000
    with env.vm.expect_revert(msg):
        env.c.create_policy(plot, coverage)
    env.vm.value = 0


class TestUnderwritingHardening:
    def test_genesis_plots_verify_against_independent_manifest(self, raw_env):
        """Deployed plots start UNVERIFIED and uninsurable. The manifest here is
        built by conftest from its own copy of the registry and its own SHA-256
        implementation, so a drift in the contract's constants or digest fails."""
        e = raw_env
        for p in e.c.list_plots():
            assert p["provenance_verified"] is False and p["provenance_manifest_sha256"] == ""
        _expect_buy_revert(e, e.farmer, "CV-ALMOND-01", "ERR_PROVENANCE_UNVERIFIED")

        records = genesis_records(e.farmer_hex)
        mock_manifest(e.vm, records)
        e.vm.sender = e.lp  # verification is permissionless
        for pid, (_n, _c, lat, lon, monthly, cs, ce, station) in GENESIS.items():
            res = e.c.verify_plot_provenance(pid)
            assert res["verified"] is True and res["code"] == "VERIFIED"
            plot = e.c.get_plot(pid)
            want = cadastral_digest(pid, lat, lon, monthly, cs, ce, GENESIS_MAX_COVERAGE, station)
            assert plot["cadastral_hash"] == want
            assert plot["provenance_verified"] is True
            assert plot["provenance_manifest_sha256"] == hashlib.sha256(manifest_body(records).encode()).hexdigest()
            assert plot["eligible_policyholder"].lower() == e.farmer_hex.lower()
            assert plot["ghcnd_station"] == station
        assert e.vm.run_validator() is True  # a validator re-reading the manifest agrees

    def test_unverified_plot_provenance_cannot_be_insured(self, raw_env):
        """An admin-typed plot is not insurable until validators have matched it,
        field by field, to the independent manifest. Every kind of mismatch or
        outage leaves it unverified, and writes nothing."""
        e = raw_env
        e.vm.sender = e.admin
        good = stage(e.c, "FARM-X", FLAT, e.farmer_hex)
        warp_to(e.vm, T0 + timedelta(days=7, minutes=1))
        e.c.activate_plot("FARM-X")  # active, but still NOT verified
        _expect_buy_revert(e, e.farmer, "FARM-X", "ERR_PROVENANCE_UNVERIFIED")

        def attempt(records=None, status=200):
            mock_manifest(e.vm, records if records is not None else {"FARM-X": good}, status)
            e.vm.sender = e.lp
            return e.c.verify_plot_provenance("FARM-X")

        tampered = {
            "MISMATCH_LATITUDE": {**good, "latitude_e4": good["latitude_e4"] + 1},
            "MISMATCH_LONGITUDE": {**good, "longitude_e4": good["longitude_e4"] - 1},
            "MISMATCH_BASELINES": {**good, "monthly_baselines": [31] + FLAT[1:]},
            "MISMATCH_CROP_CYCLE_START": {**good, "crop_cycle_start_month": 2},
            "MISMATCH_CROP_CYCLE_END": {**good, "crop_cycle_end_month": 11},
            "MISMATCH_MAX_PLOT_COVERAGE": {**good, "max_plot_coverage": str(TEST_MAX_COVERAGE * 2)},
            "MISMATCH_GHCND_STATION": {**good, "ghcnd_station": "OTHER000001"},
            "MISMATCH_ELIGIBLE_POLICYHOLDER": {**good, "eligible_policyholder": "0x" + "ab" * 20},
            "MISMATCH_CADASTRAL_HASH": {**good, "cadastral_hash": "e" * 64},
        }
        for code, rec in tampered.items():
            res = attempt({"FARM-X": rec})
            assert res["verified"] is False and res["code"] == code, (code, res)
        assert attempt({})["code"] == "PLOT_NOT_IN_MANIFEST"
        assert attempt(status=404)["code"] == "MANIFEST_UNAVAILABLE"
        e.vm.clear_mocks()
        e.vm.mock_web(URL_MANIFEST_RE, {"status": 200, "body": "not json"})
        assert e.c.verify_plot_provenance("FARM-X")["code"] == "MANIFEST_UNREADABLE"
        e.vm.clear_mocks()
        e.vm.mock_web(URL_MANIFEST_RE, {"status": 200, "body": '{"schema": "other", "plots": {}}'})
        assert e.c.verify_plot_provenance("FARM-X")["code"] == "MANIFEST_SCHEMA"
        assert e.c.get_plot("FARM-X")["provenance_verified"] is False
        _expect_buy_revert(e, e.farmer, "FARM-X", "ERR_PROVENANCE_UNVERIFIED")

        # A leader cannot ratify a mismatch: validators re-read the manifest.
        mock_manifest(e.vm, {"FARM-X": tampered["MISMATCH_BASELINES"]})
        e.vm.sender = e.lp
        e.c.verify_plot_provenance("FARM-X")
        forged = {"verified": True, "code": "VERIFIED", "manifest_sha256": "a" * 64}
        assert e.vm.run_validator(leader_result=forged) is False
        assert e.vm.run_validator(leader_result={"verified": True}) is False

        # The matching manifest verifies it, and only then is cover possible.
        res = attempt({"FARM-X": good})
        assert res["verified"] is True
        assert e.c.get_plot("FARM-X")["provenance_verified"] is True
        assert e.c.verify_plot_provenance("FARM-X")["code"] == "ALREADY_VERIFIED"
        assert buy(e.c, e.vm, e.farmer, 10 * ATTO, plot="FARM-X").startswith("P-")

    def test_cadastral_hash_must_commit_to_the_staged_record(self, env):
        """The stored hash is a SHA-256 commitment to the parcel record. An admin
        cannot stage a plot with a hash that does not describe it."""
        env.vm.sender = env.admin
        with env.vm.expect_revert("does not commit"):
            env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, "c" * 64, 1, 12, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)
        digest = cadastral_digest("BADHASH", 300000, -1000000, FLAT, 1, 12, TEST_MAX_COVERAGE, TEST_STATION)
        with env.vm.expect_revert("does not commit"):  # same hash, different coverage cap
            env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, digest, 1, 12, TEST_MAX_COVERAGE * 2, env.farmer_hex, TEST_STATION)
        with env.vm.expect_revert("eligible_policyholder"):
            env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, digest, 1, 12, TEST_MAX_COVERAGE, "not-an-address", TEST_STATION)
        with env.vm.expect_revert("invalid eligible_policyholder"):  # the admin can never be the insured
            env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, digest, 1, 12, TEST_MAX_COVERAGE, _hex(env, env.admin), TEST_STATION)
        with env.vm.expect_revert("ghcnd_station"):
            env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, digest, 1, 12, TEST_MAX_COVERAGE, env.farmer_hex, "bad station")
        env.c.stage_plot("BADHASH", "x", "x", 300000, -1000000, FLAT, digest, 1, 12, TEST_MAX_COVERAGE, env.farmer_hex, TEST_STATION)

    def test_ineligible_policyholder_rejected_on_create_policy(self, env):
        """Insurable interest: a plot is bound to its operator. A third-party
        speculator cannot buy cover on someone else's farmland, on a test plot
        or a genesis plot, whatever they are willing to pay."""
        for who in (env.lp, env.admin):
            for plot in (PLOT, "AND-OLIVE-01"):
                warp_to(env.vm, datetime(2026, 10, 15, tzinfo=timezone.utc))
                env.vm.sender = who
                env.vm.value = 10 * ATTO * 800 // 10000
                want = "Admin cannot hold" if who == env.admin else "ERR_INELIGIBLE_POLICYHOLDER"
                with env.vm.expect_revert(want):
                    env.c.create_policy(plot, 10 * ATTO)
        env.vm.value = 0
        assert env.c.get_pool()["total_liabilities"] == "0"
        assert env.c.list_policies("") == []
        # The registered operator is accepted on the same plot.
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="AND-OLIVE-01").startswith("P-")
        assert env.c.get_plot("AND-OLIVE-01")["eligible_policyholder"].lower() == env.farmer_hex.lower()

    def test_duplicate_overlapping_coverage_on_same_plot_rejected(self, env):
        """One live coverage window per plot, and unresolved coverage on the plot
        can never exceed max_plot_coverage."""
        e = env
        e.vm.sender = e.admin
        stage(e.c, "CAPPED-01", FLAT, e.farmer_hex, max_cov=15 * ATTO)
        # Staged at T0 - 0; ready after the timelock and a manifest check.
        warp_to(e.vm, T0 + timedelta(days=7, minutes=1))
        e.c.activate_plot("CAPPED-01")
        mock_manifest(e.vm, {"CAPPED-01": manifest_record("CAPPED-01", 300000, -1000000, FLAT, 1, 12, 15 * ATTO, TEST_STATION, e.farmer_hex)})
        assert e.c.verify_plot_provenance("CAPPED-01")["verified"] is True
        start = T0 + timedelta(days=7, minutes=2)
        warp_to(e.vm, start)
        assert buy(e.c, e.vm, e.farmer, 10 * ATTO, plot="CAPPED-01") == "P-1"
        pool = e.c.get_pool()
        assert e.c.get_plot("CAPPED-01")["active_coverage"] == str(10 * ATTO)
        assert e.c.get_plot("CAPPED-01")["coverage_locked"] is True

        # Same window, overlapping window, last second of the window: all refused.
        for dt in (timedelta(0), timedelta(days=1), timedelta(days=45), timedelta(days=90)):
            warp_to(e.vm, start + dt)
            _expect_buy_revert(e, e.farmer, "CAPPED-01", "ERR_DUPLICATE_PLOT_COVERAGE", coverage=1 * ATTO)
        # Refusals wrote nothing.
        assert e.c.get_pool()["total_liabilities"] == pool["total_liabilities"]
        assert e.c.get_plot("CAPPED-01")["active_coverage"] == str(10 * ATTO)

        # Once the first window has ended the plot may be covered again, but the
        # unresolved coverage (P-1 is still awaiting its verdict) counts to the cap.
        warp_to(e.vm, start + timedelta(days=90, seconds=1))
        assert e.c.get_plot("CAPPED-01")["coverage_locked"] is False
        _expect_buy_revert(e, e.farmer, "CAPPED-01", "ERR_DUPLICATE_PLOT_COVERAGE", coverage=6 * ATTO)
        assert buy(e.c, e.vm, e.farmer, 5 * ATTO, plot="CAPPED-01") == "P-2"  # 10 + 5 == cap
        assert e.c.get_plot("CAPPED-01")["active_coverage"] == str(15 * ATTO)

        # Settling P-1 releases its coverage from the plot's running total.
        warp_to(e.vm, start + timedelta(days=100, hours=3))
        mock_sources(e.vm, (NORMAL, NORMAL), obs_end=datetime.fromtimestamp(
            e.c.get_policy("P-1")["observation_end"], tz=timezone.utc))
        e.vm.sender = e.farmer
        assert e.c.evaluate_claim("P-1")["outcome"] == "NORMAL"
        assert e.c.get_plot("CAPPED-01")["active_coverage"] == str(5 * ATTO)

    def test_expiry_releases_plot_coverage(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        assert env.c.get_plot(PLOT)["active_coverage"] == str(10 * ATTO)
        warp_to(env.vm, EXPIRED_AT)
        env.c.expire_policy(pid)
        assert env.c.get_plot(PLOT)["active_coverage"] == "0"
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO).startswith("P-")  # window long over

    def test_maturity_lag_is_ten_days_after_the_last_observed_day(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        pol = env.c.get_policy(pid)
        assert pol["matures_at"] == pol["observation_end"] + DAY + MATURITY_LAG_DAYS * DAY
        warp_to(env.vm, datetime.fromtimestamp(pol["matures_at"] - 1, tz=timezone.utc))
        uniform(env.vm, TIER3)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("not matured"):
            env.c.evaluate_claim(pid)
        warp_to(env.vm, datetime.fromtimestamp(pol["matures_at"], tz=timezone.utc))
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3


URL_MANIFEST_RE = r"raw\.githubusercontent\.com"


class TestWeatherIndependence:
    def test_three_weather_sources_enforce_distinct_hostnames(self, env):
        mod = _contract_module()
        sources = env.c.get_weather_sources()
        hosts = [s["host"] for s in sources]
        assert len(sources) == 3 and len(set(hosts)) == 3
        assert set(hosts) == {"archive-api.open-meteo.com", "power.larc.nasa.gov", "www.ncei.noaa.gov"}
        assert len({s["operator_domain"] for s in sources}) == 3  # three operators, not three hostnames of one
        assert all(s["independent"] for s in sources)

        urls = mod._source_urls(366000, -1198000, OBS_START, OBS_END, "USW00093193")
        assert [n for n, _ in urls] == ["open-meteo-era5", "nasa-power", "noaa-ncei"]
        assert mod._sources_independent(urls) is True
        assert len({mod._host_of(u) for _, u in urls}) == 3
        # The old design: two hostnames of ONE operator. Rejected.
        old = [urls[0], ("noaa-gfs-via-open-meteo", "https://api.open-meteo.com/v1/forecast?x=1"), urls[1]]
        assert len({mod._host_of(u) for _, u in old}) == 3  # hostnames differ ...
        assert mod._sources_independent(old) is False  # ... the operator does not
        same_host = [urls[0], urls[1], ("dup", "https://power.larc.nasa.gov/other")]
        assert mod._sources_independent(same_host) is False
        sub = [urls[0], urls[1], ("sub", "https://data.larc.nasa.gov/x")]
        assert mod._sources_independent(sub) is False
        assert mod._sources_independent(urls[:2]) is False
        assert mod._sources_independent([urls[0], urls[1], ("http", "http://www.ncei.noaa.gov/x")]) is False

    def test_contract_refuses_a_correlated_source_plan(self, env, monkeypatch):
        """If the plan ever has two sources on one operator, nothing is fetched
        and nothing settles, rather than counting one operator twice."""
        mod = _contract_module()
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER3)
        real = mod._source_urls

        def correlated(lat, lon, a, b, station):
            u = real(lat, lon, a, b, station)
            return [u[0], u[1], ("noaa-gfs-via-open-meteo", "https://api.open-meteo.com/v1/forecast?x=1")]

        monkeypatch.setattr(mod, "_source_urls", correlated)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("distinct hosts and operators"):
            env.c.evaluate_claim(pid)
        assert env.c.get_policy(pid)["status"] == "ACTIVE"
        assert env.c.get_claim(pid) == {}

    def test_station_gauge_is_a_real_third_vote(self, env):
        """The NOAA gauge is wired in as the third source: it can outvote the
        reanalysis pair, and a gauge with a missing day abstains, never reads 0."""
        pid = buy(env.c, env.vm, env.farmer)
        pid2 = buy(env.c, env.vm, env.farmer, plot=EXTRA_PLOTS[0])
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (NORMAL, NORMAL), ncei=(TIER3, TIER3), nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and claim["source_count"] == 3  # NASA + NCEI outvote ERA5

        short = [r for r in json.loads(_ncei_body(OBS_END, TIER3, TIER3)) if not r["DATE"].endswith("-05")]
        assert len(short) == 27
        env.vm.clear_mocks()
        env.vm.mock_web(URL_OM, {"status": 200, "body": _om_body(OBS_END, TIER3, TIER3)})
        env.vm.mock_web(URL_NASA, {"status": 200, "body": _nasa_body(OBS_END, TIER3, TIER3)})
        env.vm.mock_web(URL_NCEI, {"status": 200, "body": json.dumps(short)})
        claim2 = env.c.evaluate_claim(pid2)
        assert claim2["source_count"] == 2 and claim2["payout_tier"] == 3


class TestBoundaryAndRevisions:
    def test_validators_straddling_tier_boundary_converge_without_liveness_deadlock(self, env):
        """Baseline per window is 14.0 mm. 7.0 mm is a 50% deficit (tier 2) and
        7.1 mm is 49% (tier 1): two honest validators on either side of the
        threshold, within the noise tolerance. Under strict tier equality every
        leader would be rejected by the other side forever."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        env.vm.sender = env.farmer
        hi = {"tier": 2, "median_deficit": 50, "source_count": 3, "reasoning": "x"}
        lo = {"tier": 1, "median_deficit": 49, "source_count": 3, "reasoning": "x"}

        # Seed a captured validator: settle nothing yet (outage), just capture.
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)
        env.vm.sender = env.lp
        env.c.evaluate_claim(pid)

        def sees(mm):
            uniform(env.vm, mm)

        # Validator A sees 50% (tier 2), validator B sees 49% (tier 1).
        sees(7.0)
        assert env.vm.run_validator(leader_result=hi) is True  # A accepts a leader at its own tier
        assert env.vm.run_validator(leader_result=lo) is True  # A accepts the LOWER tier (49 <= 50)
        sees(7.1)
        assert env.vm.run_validator(leader_result=hi) is False  # B will not ratify a tier above its own ...
        assert env.vm.run_validator(leader_result=lo) is True  # ... but ratifies A's lower one: converged

        # What a leader may NOT do: skip past the noise band, or go above anyone.
        assert env.vm.run_validator(leader_result={**lo, "median_deficit": 40, "tier": 1}) is False  # 9 points away
        sees(7.0)
        assert env.vm.run_validator(leader_result={"tier": 3, "median_deficit": 75, "source_count": 3, "reasoning": "x"}) is False
        # A lower tier is only accepted inside the band: 24% -> tier 0 vs a validator at 50%.
        assert env.vm.run_validator(leader_result={"tier": 0, "median_deficit": 24, "source_count": 3, "reasoning": "x"}) is False

        # Settle for real with the lower-viewing leader: tier 1, 30%.
        sees(7.1)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["median_deficit"] == 49
        assert int(claim["approved_payout"]) == 3 * ATTO
        sees(7.0)
        assert env.vm.run_validator() is True  # the validator on the other side ratifies it

    def test_boundary_dispute_resolves_at_guaranteed_lower_bound(self, env):
        """The backstop: the policyholder can always settle at tier(deficit - 5)."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, 7.0)  # 50% deficit
        env.vm.sender = env.lp
        with env.vm.expect_revert("only the policyholder"):
            env.c.resolve_boundary_dispute(pid)
        env.vm.sender = env.farmer
        claim = env.c.resolve_boundary_dispute(pid)
        assert claim["median_deficit"] == 50
        assert claim["payout_tier"] == 1 and int(claim["approved_payout"]) == 3 * ATTO  # tier(45), not tier(50)
        assert claim["settlement_mode"] == "BOUNDARY_CONSERVATIVE"
        assert "lower-bound" in claim["reasoning"]
        assert env.c.get_policy(pid)["status"] == "SETTLED_PAYOUT"
        # Validators: certify the deficit within the band, and the tier must be
        # the lower bound of THAT deficit, never the plain tier.
        uniform(env.vm, 7.1)
        assert env.vm.run_validator() is True
        plain = {"tier": 2, "median_deficit": 50, "source_count": 3, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=plain) is False
        assert env.vm.run_validator(leader_result={**plain, "tier": 1}) is True
        assert env.vm.run_validator(leader_result={**plain, "tier": 0}) is False
        # Settled is settled.
        with env.vm.expect_revert("not claimable"):
            env.c.resolve_boundary_dispute(pid)

    def test_boundary_dispute_outage_is_retryable_not_final(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), ncei=None, nasa=None)
        env.vm.sender = env.farmer
        assert env.c.resolve_boundary_dispute(pid)["outcome"] == "QUORUM_FAILURE"
        assert env.c.get_policy(pid)["status"] == "ACTIVE"
        warp_to(env.vm, MATURITY + timedelta(days=1, hours=3))
        uniform(env.vm, TIER3)  # 85% -> tier(80) = 3
        assert env.c.resolve_boundary_dispute(pid)["payout_tier"] == 3

    def test_revised_observations_during_cooldown_fairly_update_settlement_tier(self, env):
        """Upstream stations revise history inside the 24h cooldown. The payout is
        re-priced, up or down, before a single wei streams, and the pool's
        accounting stays whole at every step."""
        pid, claim = claim_with(env, TIER1)  # 28% deficit -> tier 1, 3 GEN
        assert int(claim["approved_payout"]) == 3 * ATTO and claim["revisable"] is True
        cool = claim["cooldown_until"]
        pool = env.c.get_pool()
        # Only the payout left the reserve. The other 7 GEN is held for revisions.
        assert int(pool["total_liabilities"]) == 7 * ATTO and int(pool["committed_payouts"]) == 3 * ATTO
        assert claim["revision_reserve"] == str(7 * ATTO)
        base_pool = int(pool["pool_balance"])

        env.vm.sender = env.lp  # permissionless
        # Too soon after the settlement.
        uniform(env.vm, TIER3)
        with env.vm.expect_revert("revised too recently"):
            env.c.re_evaluate_revised_claim(pid)

        # The stations revise the window DOWN to a severe drought: 85% -> tier 3.
        warp_to(env.vm, datetime.fromtimestamp(claim["evaluated_at"] + 2 * 3600, tz=timezone.utc))
        res = env.c.re_evaluate_revised_claim(pid)
        assert res["revised"] is True and res["payout_tier"] == 3 and res["revisions"] == 1
        assert int(res["approved_payout"]) == 10 * ATTO
        assert res["cooldown_until"] == cool  # the cooldown is not extended
        pool = env.c.get_pool()
        assert int(pool["committed_payouts"]) == 10 * ATTO and int(pool["total_liabilities"]) == 0
        assert int(pool["pool_balance"]) == base_pool - 7 * ATTO
        assert res["revision_reserve"] == "0"
        assert int(pool["pool_balance"]) >= int(pool["total_liabilities"])

        # Revised again, this time back to normal rain: nothing is owed.
        warp_to(env.vm, datetime.fromtimestamp(claim["evaluated_at"] + 5 * 3600, tz=timezone.utc))
        uniform(env.vm, NORMAL)
        res = env.c.re_evaluate_revised_claim(pid)
        assert res["revised"] is True and res["payout_tier"] == 0 and res["approved_payout"] == "0"
        pol = env.c.get_policy(pid)
        assert pol["status"] == "SETTLED_NO_PAYOUT" and pol["claimed"] is False
        pool = env.c.get_pool()
        assert int(pool["committed_payouts"]) == 0 and int(pool["total_liabilities"]) == 0
        assert int(pool["pool_balance"]) == base_pool + 3 * ATTO  # the 3 GEN came back to the LPs
        env.vm.sender = env.farmer
        with env.vm.expect_revert("no approved payout"):
            env.c.release_payout(pid)
        with env.vm.expect_revert("not revisable"):
            env.c.re_evaluate_revised_claim(pid)

    def test_revision_noise_does_not_flip_the_payout(self, env):
        """A 1-point wobble across a threshold is noise, not a revision. A real
        revision needs the tier to change AND the deficit to move by more than the
        noise tolerance, so the payout cannot flap at a boundary."""
        pid, claim = claim_with(env, 7.0)  # 50% -> tier 2, 7 GEN
        assert claim["payout_tier"] == 2
        warp_to(env.vm, datetime.fromtimestamp(claim["evaluated_at"] + 2 * 3600, tz=timezone.utc))
        uniform(env.vm, 7.1)  # 49% -> tier 1, but only 1 point away
        env.vm.sender = env.lp
        res = env.c.re_evaluate_revised_claim(pid)
        assert res["revised"] is False and res["payout_tier"] == 2 and res["revisions"] == 0
        assert int(env.c.get_claim(pid)["approved_payout"]) == 7 * ATTO

    def test_revision_needs_all_three_sources(self, env):
        """A degraded two-source read cannot move money after settlement."""
        pid, claim = claim_with(env, TIER1)
        warp_to(env.vm, datetime.fromtimestamp(claim["evaluated_at"] + 2 * 3600, tz=timezone.utc))
        mock_sources(env.vm, (NORMAL, NORMAL), ncei=None, nasa=(NORMAL, NORMAL))
        env.vm.sender = env.lp
        assert env.c.re_evaluate_revised_claim(pid)["revised"] is False
        assert env.c.get_claim(pid)["payout_tier"] == 1

    def test_revision_window_closes_when_streaming_begins(self, env):
        pid, claim = claim_with(env, TIER1)
        warp_to(env.vm, datetime.fromtimestamp(claim["cooldown_until"], tz=timezone.utc))
        uniform(env.vm, TIER3)
        env.vm.sender = env.lp
        with env.vm.expect_revert("streaming has begun"):
            env.c.re_evaluate_revised_claim(pid)
        # The held-back reserve is released by anyone, so LP capital is not stranded.
        pool = env.c.get_pool()
        assert int(pool["total_liabilities"]) == 7 * ATTO
        env.c.finalize_claim(pid)
        assert int(env.c.get_pool()["total_liabilities"]) == 0
        assert env.c.get_claim(pid)["finalized"] is True and env.c.get_claim(pid)["revisable"] is False
        with env.vm.expect_revert("already finalized"):
            env.c.finalize_claim(pid)
        # And the payout itself is unchanged and streams as before.
        env.vm.sender = env.farmer
        warp_to(env.vm, datetime.fromtimestamp(claim["cooldown_until"] + 3 * DAY + 1, tz=timezone.utc))
        assert env.c.release_payout(pid) == 3 * ATTO

    def test_release_payout_also_finalizes_the_reserve(self, env):
        pid, claim = claim_with(env, TIER1)
        warp_to(env.vm, datetime.fromtimestamp(claim["cooldown_until"] + DAY, tz=timezone.utc))
        env.vm.sender = env.farmer
        assert env.c.release_payout(pid) > 0
        assert int(env.c.get_pool()["total_liabilities"]) == 0

    def test_revision_count_is_bounded(self, env):
        pid, claim = claim_with(env, TIER1)
        t = claim["evaluated_at"]
        tiers = [TIER2, TIER3, TIER1]  # each a material move away from the last
        env.vm.sender = env.lp
        for mm in tiers:
            t += 3600
            warp_to(env.vm, datetime.fromtimestamp(t, tz=timezone.utc))
            uniform(env.vm, mm)
            assert env.c.re_evaluate_revised_claim(pid)["revised"] is True
        t += 3600
        warp_to(env.vm, datetime.fromtimestamp(t, tz=timezone.utc))
        uniform(env.vm, TIER2)
        with env.vm.expect_revert("revision limit"):
            env.c.re_evaluate_revised_claim(pid)


class TestCommittedRegistryManifest:
    def test_committed_manifest_file_verifies_every_genesis_plot(self, direct_vm, direct_deploy, direct_alice):
        """registry/cadastral_manifest.json is the file validators fetch on the
        live network. Deploy with its operator and verify every genesis plot
        against its exact bytes."""
        import pathlib

        path = pathlib.Path(__file__).resolve().parent.parent / "registry" / "cadastral_manifest.json"
        raw = path.read_text()
        doc = json.loads(raw)
        operators = {r["eligible_policyholder"] for r in doc["plots"].values()}
        assert len(operators) == 1
        warp_to(direct_vm, T0)
        direct_vm.sender = direct_alice
        c = direct_deploy(CONTRACT, 5 * ATTO, operators.pop())
        direct_vm.mock_web(URL_MANIFEST_RE, {"status": 200, "body": raw})
        for pid in doc["plots"]:
            res = c.verify_plot_provenance(pid)
            assert res["verified"] is True, (pid, res)
            assert res["manifest_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert all(p["provenance_verified"] for p in c.list_plots())


class TestFrontendMirrorParity:
    """frontend/src/lib/underwriting.ts re-implements the contract's pure rules.
    Both sides are checked against ONE committed vector file, which this test
    regenerates from the contract's own functions (UPDATE_PARITY=1 rewrites it)."""

    @staticmethod
    def _vectors():
        mod = _contract_module()
        tiers = [mod._tier_for_deficit(d) for d in range(101)]
        cons = [mod._conservative_tier(d) for d in range(101)]

        def pay(d):
            return {"tier": mod._tier_for_deficit(d), "median_deficit": d, "source_count": 3, "reasoning": "x"}

        agree = []
        for lead in range(101):
            row = "".join("1" if mod._validators_agree(pay(lead), pay(mine)) else "0" for mine in range(101))
            agree.append(row)
        urls = {
            "independent": [
                "https://archive-api.open-meteo.com/v1/archive?a=1",
                "https://power.larc.nasa.gov/api/x",
                "https://www.ncei.noaa.gov/access/services/data/v1?b=2",
            ],
            "two_hostnames_one_operator": [
                "https://archive-api.open-meteo.com/v1/archive",
                "https://api.open-meteo.com/v1/forecast",
                "https://power.larc.nasa.gov/api/x",
            ],
            "duplicate_host": [
                "https://power.larc.nasa.gov/a",
                "https://power.larc.nasa.gov/b",
                "https://www.ncei.noaa.gov/c",
            ],
            "plain_http": [
                "https://archive-api.open-meteo.com/v1/archive",
                "https://power.larc.nasa.gov/api/x",
                "http://www.ncei.noaa.gov/c",
            ],
            "two_sources": ["https://power.larc.nasa.gov/a", "https://www.ncei.noaa.gov/c"],
        }
        hosts = {k: mod._sources_independent([(f"s{i}", u) for i, u in enumerate(v)]) for k, v in urls.items()}
        return {"tier": tiers, "conservative": cons, "agree": agree, "urls": urls, "independent": hosts}

    def test_vectors_match_committed_file_and_frontend(self, env):
        import os
        import pathlib

        path = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "tests" / "parity_vectors.json"
        got = self._vectors()
        if os.environ.get("UPDATE_PARITY") == "1":
            path.write_text(json.dumps(got, indent=1) + "\n")
        assert json.loads(path.read_text()) == got


class TestViewReturnsAreCalldataEncodable:
    """Direct mode never encodes return values, but GenVM does: a float (or any
    non-calldata type) in a view result makes the call fail on-chain with
    'not calldata encodable'. This is the bug that broke every list_plots /
    get_plot readback on Studio Next."""

    @staticmethod
    def _check(v, path="result"):
        if v is None or isinstance(v, (bool, int, str, bytes)):
            return
        if isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                TestViewReturnsAreCalldataEncodable._check(x, f"{path}[{i}]")
            return
        if isinstance(v, dict):
            for k, x in v.items():
                assert isinstance(k, str), f"{path}: non-str key {k!r}"
                TestViewReturnsAreCalldataEncodable._check(x, f"{path}.{k}")
            return
        raise AssertionError(f"{path}: not calldata encodable: {type(v).__name__} {v!r}")

    def test_every_view_and_write_result_is_encodable(self, env):
        c = env.c
        pid, claim = claim_with(env, TIER1)
        env.vm.sender = env.lp
        revised = c.re_evaluate_revised_claim  # exercised below after a wait
        views = {
            "get_config": c.get_config(),
            "get_weather_sources": c.get_weather_sources(),
            "get_pool": c.get_pool(),
            "get_plot": c.get_plot("CV-ALMOND-01"),
            "list_plots": c.list_plots(),
            "get_policy": c.get_policy(pid),
            "list_policies": c.list_policies(""),
            "get_claim": c.get_claim(pid),
            "get_lp": c.get_lp(_hex(env, env.lp)),
            "evaluate_claim": claim,
        }
        for name, val in views.items():
            self._check(val, name)
        warp_to(env.vm, datetime.fromtimestamp(claim["evaluated_at"] + 2 * 3600, tz=timezone.utc))
        uniform(env.vm, TIER3)
        self._check(revised(pid), "re_evaluate_revised_claim")
        self._check(c.verify_plot_provenance("CV-ALMOND-01"), "verify_plot_provenance")
        assert c.get_plot("CV-ALMOND-01")["latitude"] == "36.6000"
        assert c.get_plot("AND-OLIVE-01")["longitude"] == "-4.3700"
