"""AetherClaim: lifecycle, adversarial, and audit regression suite (direct mode).

Run: python -m pytest tests/test_aether_claim.py -v
"""

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
    FLAT,
    HASH,
    MATURITY,
    NORMAL,
    OBS_END,
    OBS_START,
    PLOT,
    POLICY_DAYS,
    SEASONAL,
    SEASONAL_PLOT,
    T0,
    TEST_PLOT,
    TIER1,
    TIER2,
    TIER3,
    buy,
    mock_sources,
    provide,
    uniform,
    warp_to,
)

CV = "CV-ALMOND-01"


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    # Test plots are staged 9 days before T0 and activated 8 days later, clear of
    # the 7-day staging timelock.
    warp_to(direct_vm, T0 - timedelta(days=9))
    direct_vm.sender = direct_alice  # deployer == admin
    c = direct_deploy(CONTRACT, 500 * ATTO)
    test_plots = [
        (TEST_PLOT, FLAT),
        (ARID_PLOT, ARID),
        (SEASONAL_PLOT, SEASONAL),
        (ARID_MAR_PLOT, ARID_MAR),
        (ARID_APR_PLOT, ARID_APR),
    ]
    for pid, monthly in test_plots:
        c.stage_plot(pid, f"Test {pid}", "TEST", 300000, -1000000, monthly, HASH, 1, 12)
    warp_to(direct_vm, T0 - timedelta(days=1))
    for pid, _ in test_plots:
        c.activate_plot(pid)
    warp_to(direct_vm, T0)
    provide(c, direct_vm, direct_bob, 100 * ATTO)

    class E:
        pass

    e = E()
    e.vm, e.c = direct_vm, c
    e.admin, e.lp, e.farmer = direct_alice, direct_bob, direct_charlie
    return e


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
        env.c.stage_plot("NEW-01", "Test Orchard", "PECAN", 300000, -1000000, FLAT, HASH, 3, 10)
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
        env.c.stage_plot("NEW-02", "Staged", "OLIVE", 300000, -50000, FLAT, "b" * 64, 1, 12)
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("not active"):
            env.c.create_policy("NEW-02", 10**18)

    def test_only_admin_registers_plots(self, env):
        env.vm.sender = env.farmer
        with env.vm.expect_revert("only admin"):
            env.c.stage_plot("EVIL", "x", "x", 0, 0, FLAT, "c" * 64, 1, 12)

    def test_single_station_fault_cannot_trigger_payout(self, env):
        """One station reports 0 mm (tier 3) while two report normal rain. The
        median is tier 0, so the isolated reading cannot pay anything."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (0.0, 0.0), gfs=(NORMAL, NORMAL), nasa=(NORMAL, NORMAL))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "NORMAL" and claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"
        warp_to(env.vm, MATURITY + timedelta(days=2))
        mock_sources(env.vm, (TIER3, TIER3), gfs=(TIER3, TIER3), nasa=(TIER3, TIER3))
        with env.vm.expect_revert("not claimable"):
            env.c.evaluate_claim(pid)  # settled is final: no second look

    def test_source_disagreement_settles_at_conservative_lower_tier(self, env):
        """Audit PoC. Open-Meteo (ERA5) reads Tier 3, NASA POWER reads Tier 1, and
        the GFS leg is down. Two sources answer, so the lower tier settles: Tier 1
        (30%), deterministically. It does not settle at 0, and it does not pay 100%."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=None, nasa=(TIER1, TIER1))
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

    def test_three_source_split_settles_at_median(self, env):
        """All three answer: Tier 3, Tier 1, Tier 1. The median is Tier 1."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=(TIER1, TIER1), nasa=(TIER1, TIER1))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 1 and claim["source_count"] == 3
        assert claim["reasoning"].startswith("3 sources answered: median tier 1")

    def test_single_low_outlier_is_outvoted(self, env):
        """Two sources read Tier 3, one reads normal rain. The median is Tier 3, so
        one faulty low reading cannot wipe out a drought that two sources see.
        Note that the two Open-Meteo legs count as two votes (see README)."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=(TIER3, TIER3), nasa=(NORMAL, NORMAL))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and int(claim["approved_payout"]) == 10 * ATTO

    def test_two_source_split_settles_at_lower_reading_even_if_zero(self, env):
        """Documented limitation. With only two sources answering, the lower reading
        governs, so one normal-rain reading can settle a drought at zero. The
        three-source median does not have this weakness."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=(NORMAL, NORMAL), nasa=None)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 0 and claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"

    def test_too_few_sources_is_quorum_failure_and_retryable(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=None, nasa=None)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "QUORUM_FAILURE" and claim["source_count"] == 1
        assert env.c.get_policy(pid)["status"] == "ACTIVE"  # still open for a retry

    def test_two_of_three_sources_can_settle(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        mock_sources(env.vm, (TIER3, TIER3), gfs=None, nasa=(TIER3, TIER3))
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
        Tier 1. Exact-tier equivalence rejects it."""
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, 7.0)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 2
        uniform(env.vm, 7.1)
        assert env.vm.run_validator() is False

    def test_validator_accepts_same_tier_despite_value_noise(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, MATURITY + timedelta(hours=3))
        uniform(env.vm, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        uniform(env.vm, 5.4)  # different reading, same bucket
        assert env.vm.run_validator() is True

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
                mock_sources(env.vm, pair(mms[0]), gfs=pair(mms[1]), nasa=pair(mms[2]))
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
        mock_sources(env.vm, (TIER3, TIER3), gfs=None, nasa=None)  # one source only
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
        mock_sources(env.vm, (TIER3, TIER3), gfs=None, nasa=None)  # outage
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
            env.c.create_policy(PLOT, 6 * ATTO)

    def test_solvency_cap(self, env):
        env.vm.sender = env.admin
        env.c.set_max_epoch_exposure(10_000 * ATTO)
        for _ in range(4):  # 4 x 25 = 100 of ~108 pool
            buy(env.c, env.vm, env.farmer, 25 * ATTO)
        env.vm.sender = env.farmer
        env.vm.value = 25 * ATTO * 800 // 10000
        with env.vm.expect_revert("solvency"):
            env.c.create_policy(PLOT, 25 * ATTO)

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
        warp_to(env.vm, datetime(2026, 4, 16, 3, tzinfo=timezone.utc))  # term end + 6 days
        mock_sources(env.vm, (5.0, 10.0), obs_end=obs_end)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 2

    def test_stage_plot_validates_cycle_and_baselines(self, env):
        env.vm.sender = env.admin
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B1", "x", "x", 0, 0, FLAT, HASH, 0, 6)
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B2", "x", "x", 0, 0, FLAT, HASH, 3, 13)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B3", "x", "x", 0, 0, FLAT[:11], HASH, 3, 10)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B4", "x", "x", 0, 0, [-1] + FLAT[1:], HASH, 3, 10)

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
        warp_to(env.vm, datetime(2027, 2, 5, 3, tzinfo=timezone.utc))  # term end + 6 days
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
        env.c.stage_plot("ADM-01", "Admin Farm", "PECAN", 300000, -1000000, FLAT, "d" * 64, 1, 12)
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
        mock_sources(env.vm, (TIER3, TIER3), gfs=(TIER1, TIER1), nasa=(TIER1, TIER1))
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        assert env.vm.run_validator(leader_result={"tier": 1, "median_deficit": 28, "source_count": 3, "reasoning": "x"}) is True
        assert env.vm.run_validator(leader_result={"tier": 3, "median_deficit": 85, "source_count": 3, "reasoning": "x"}) is False
        assert env.vm.run_validator(leader_result={"tier": 0, "median_deficit": 0, "source_count": 3, "reasoning": "x"}) is False
        relabelled = {"tier": -2, "median_deficit": 0, "source_count": 1, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=relabelled) is False

    def test_stored_reasoning_is_canonical_not_leader_text(self, env):
        _, claim = claim_with(env, TIER2)
        assert claim["reasoning"] == "3 sources answered: median tier 2 (SEVERE_DROUGHT), median sustained deficit 64%"
