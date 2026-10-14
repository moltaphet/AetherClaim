"""AetherClaim: lifecycle + adversarial direct-mode suite."""

import pytest

from conftest import (
    ATTO, CONTRACT, DAY, NORMAL, PLOT, T0, TIER1, TIER2, TIER3,
    buy, eval_time, mock_sources, provide, uniform, warp_to,
)
from datetime import datetime, timedelta, timezone

MONTHLY = [20, 20, 30, 30, 30, 10, 5, 5, 10, 20, 30, 20]


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    warp_to(direct_vm, T0)
    direct_vm.sender = direct_alice  # deployer == admin
    c = direct_deploy(CONTRACT, 500 * ATTO)
    provide(c, direct_vm, direct_bob, 100 * ATTO)

    class E:
        pass

    e = E()
    e.vm, e.c = direct_vm, c
    e.admin, e.lp, e.farmer = direct_alice, direct_bob, direct_charlie
    return e


def claim_with(e, mm, pid=None, days=40):
    pid = pid or buy(e.c, e.vm, e.farmer)
    now = eval_time(days)
    warp_to(e.vm, now)
    uniform(e.vm, now, mm)
    e.vm.sender = e.farmer
    return pid, e.c.evaluate_claim(pid)


# ----------------------------------------------------------- normal lifecycle
class TestLifecycle:
    def test_genesis_registry(self, env):
        plots = env.c.list_plots()
        assert {p["plot_id"] for p in plots} == {"CV-ALMOND-01", "IA-CORN-01", "AND-OLIVE-01"}
        assert all(p["active"] and len(p["cadastral_hash"]) == 64 for p in plots)
        assert all(len(p["monthly_baselines"]) == 12 for p in plots)
        cv = env.c.get_plot("CV-ALMOND-01")
        assert (cv["crop_cycle_start_month"], cv["crop_cycle_end_month"]) == (2, 11)
        assert env.c.get_plot("AND-OLIVE-01")["crop_cycle_start_month"] == 10  # wraps year end

    def test_stage_then_activate_after_delay(self, env):
        env.vm.sender = env.admin
        env.c.stage_plot("NEW-01", "Test Orchard", "PECAN", 300000, -1000000, MONTHLY, "a" * 64, 3, 10)
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
        [(TIER1, 1, "MODERATE_DROUGHT", 3000), (TIER2, 2, "SEVERE_DROUGHT", 7000),
         (TIER3, 3, "CATASTROPHIC_DROUGHT", 10000)],
    )
    def test_tiered_payouts(self, env, mm, tier, label, bps):
        pid, claim = claim_with(env, mm)
        assert claim["payout_tier"] == tier and claim["outcome"] == label
        assert claim["source_count"] == 3
        assert int(claim["approved_payout"]) == 10 * ATTO * bps // 10000
        assert env.c.get_policy(pid)["claimed"] is True
        assert int(env.c.get_pool()["committed_payouts"]) == int(claim["approved_payout"])

    def test_normal_rainfall_pays_nothing(self, env):
        pid, claim = claim_with(env, NORMAL)
        assert claim["outcome"] == "NORMAL" and claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["claimed"] is False

    def test_linear_streaming_release(self, env):
        pid, claim = claim_with(env, TIER3)
        payout = int(claim["approved_payout"])
        cool = claim["cooldown_until"]
        # Half way through vesting: 1.5 days after cooldown ends.
        from datetime import datetime, timezone
        warp_to(env.vm, datetime.fromtimestamp(cool + int(1.5 * DAY), tz=timezone.utc))
        released = env.c.release_payout(pid)
        assert released == payout // 2
        assert int(env.c.get_claim(pid)["vested_amount"]) == payout // 2
        # Fully vested.
        warp_to(env.vm, datetime.fromtimestamp(cool + 3 * DAY + 5, tz=timezone.utc))
        released2 = env.c.release_payout(pid)
        assert released + released2 == payout
        assert int(env.c.get_pool()["committed_payouts"]) == 0
        with env.vm.expect_revert("nothing vested"):
            env.c.release_payout(pid)

    def test_expire_releases_reserve(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, T0 + timedelta(days=90 + 15))
        env.c.expire_policy(pid)
        assert int(env.c.get_pool()["total_liabilities"]) == 0


# ----------------------------------------------------------------- adversarial
class TestAdversarialProtection:
    def test_arbitrary_plot_rejected(self, env):
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("authenticated registry"):
            env.c.create_policy("MY-OWN-FARM-0,0", 10**18)

    def test_staged_plot_not_insurable_until_active(self, env):
        env.vm.sender = env.admin
        env.c.stage_plot("NEW-02", "Staged", "OLIVE", 300000, -50000, MONTHLY, "b" * 64, 1, 12)
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("not active"):
            env.c.create_policy("NEW-02", 10**18)

    def test_only_admin_registers_plots(self, env):
        env.vm.sender = env.farmer
        with env.vm.expect_revert("only admin"):
            env.c.stage_plot("EVIL", "x", "x", 0, 0, MONTHLY, "c" * 64, 1, 12)

    def test_single_source_fault_is_inconclusive(self, env):
        """One station reports 0 mm, two report normal: no payout."""
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        mock_sources(env.vm, now, om=(0.0, 0.0), gfs=(NORMAL, NORMAL), nasa=(NORMAL, NORMAL))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "INCONCLUSIVE"
        assert claim["approved_payout"] == "0"
        assert env.c.get_policy(pid)["claimed"] is False

    def test_too_few_sources_is_inconclusive(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        mock_sources(env.vm, now, om=(TIER3, TIER3), gfs=False, nasa=False)
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["outcome"] == "INCONCLUSIVE" and claim["source_count"] == 1

    def test_two_of_three_sources_can_settle(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        mock_sources(env.vm, now, om=(TIER3, TIER3), gfs=False, nasa=(TIER3, TIER3))
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 3 and claim["source_count"] == 2

    def test_single_window_anomaly_pays_nothing(self, env):
        """Drought in one window only: sustained-deviation rule yields tier 0."""
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        mock_sources(env.vm, now, om=(TIER3, NORMAL), gfs=(TIER3, NORMAL), nasa=(TIER3, NORMAL))
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["outcome"] == "NORMAL"

    def test_sustained_tier_is_weakest_window(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        mock_sources(env.vm, now, om=(TIER3, TIER1), gfs=(TIER3, TIER1), nasa=(TIER3, TIER1))
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 1

    def test_economic_drift_rejected_by_validator(self, env):
        """Leader proposes Tier 2; the validator's own sources land in Tier 1
        (7.1 mm = 49% deficit). Exact-tier equivalence rejects it."""
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, 7.0)  # exactly 50% -> Tier 2
        env.vm.sender = env.farmer
        claim = env.c.evaluate_claim(pid)
        assert claim["payout_tier"] == 2
        uniform(env.vm, now, 7.1)  # 49% -> Tier 1
        assert env.vm.run_validator() is False

    def test_validator_accepts_same_tier_despite_value_noise(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        uniform(env.vm, now, 5.4)  # different reading, same bucket
        assert env.vm.run_validator() is True

    def test_validator_rejects_leader_error(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        assert env.vm.run_validator(leader_error=Exception("boom")) is False

    def test_validator_rejects_fabricated_tier3(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, NORMAL)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        fake = {"tier": 3, "median_deficit": 99, "source_count": 3, "reasoning": "x"}
        assert env.vm.run_validator(leader_result=fake) is False

    def test_cooldown_blocks_instant_drain(self, env):
        pid, claim = claim_with(env, TIER3)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("TIMELOCK"):
            env.c.release_payout(pid)

    def test_policy_bought_after_drought_cannot_claim(self, env):
        """Adverse selection: windows must lie inside the coverage period."""
        now = T0 + timedelta(days=60)
        warp_to(env.vm, now)
        pid = buy(env.c, env.vm, env.farmer)
        warp_to(env.vm, now + timedelta(days=5))
        uniform(env.vm, now + timedelta(days=5), TIER3)
        env.vm.sender = env.farmer
        with env.vm.expect_revert("TIMELOCK"):
            env.c.evaluate_claim(pid)

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
        buy(env.c, env.vm, env.farmer, 20 * ATTO)
        env.vm.sender = env.lp
        # Burning every share pays out only UNRESERVED capital: pool 101.6,
        # reserve 20 -> 81.6 leaves. The 20 GEN reserve stays behind.
        out = env.c.withdraw_liquidity(100 * ATTO)
        assert out == 101_600_000_000_000_000_000 - 20 * ATTO
        pool = env.c.get_pool()
        assert int(pool["pool_balance"]) >= int(pool["total_liabilities"]) == 20 * ATTO

    def test_evaluation_rate_limited_and_single_claim(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, NORMAL)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        with env.vm.expect_revert("too recently"):
            env.c.evaluate_claim(pid)
        later = now + timedelta(days=2)
        warp_to(env.vm, later)
        uniform(env.vm, later, TIER3)
        assert env.c.evaluate_claim(pid)["payout_tier"] == 3
        with env.vm.expect_revert("not claimable"):
            env.c.evaluate_claim(pid)


# ----------------------------------------------------- audit regression tests
def _hex(env, who):
    env.vm.sender = who
    return env.c.whoami()


class TestAuditRegressions:
    # Issue #1: seasonal baseline / crop cycle ---------------------------------
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
        # Same plot, bought inside the cycle with the whole term in-season: ok.
        warp_to(env.vm, datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="IA-CORN-01").startswith("P-")

    def test_wrapping_cycle_accepts_winter_term(self, env):
        warp_to(env.vm, datetime(2026, 10, 15, tzinfo=timezone.utc))  # Oct..Jan, in Oct-Mar
        assert buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="AND-OLIVE-01").startswith("P-")

    def test_per_month_baselines_drive_the_deficit(self, env):
        """Window 1 is judged against April (95 mm/month), window 2 against May
        (120 mm/month): 22.0 mm and 28.0 mm are each a 50% deficit. A single
        scalar baseline could not make both windows land in the same tier."""
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        warp_to(env.vm, start)
        pid = buy(env.c, env.vm, env.farmer, 10 * ATTO, plot="IA-CORN-01")
        now = start + timedelta(days=45, hours=3)
        warp_to(env.vm, now)
        uniform_pair = (22.0, 28.0)
        mock_sources(env.vm, now, om=uniform_pair, gfs=uniform_pair, nasa=uniform_pair)
        env.vm.sender = env.farmer
        assert env.c.evaluate_claim(pid)["payout_tier"] == 2

    def test_stage_plot_validates_cycle_and_baselines(self, env):
        env.vm.sender = env.admin
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B1", "x", "x", 0, 0, MONTHLY, "a" * 64, 0, 6)
        with env.vm.expect_revert("crop cycle month"):
            env.c.stage_plot("B2", "x", "x", 0, 0, MONTHLY, "a" * 64, 3, 13)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B3", "x", "x", 0, 0, MONTHLY[:11], "a" * 64, 3, 10)
        with env.vm.expect_revert("monthly_baselines"):
            env.c.stage_plot("B4", "x", "x", 0, 0, [-1] + MONTHLY[1:], "a" * 64, 3, 10)

    # Issue #2: admin self-dealing ---------------------------------------------
    def test_admin_self_insurance_prevented(self, env):
        env.vm.sender = env.admin
        env.c.stage_plot("ADM-01", "Admin Farm", "PECAN", 300000, -1000000, MONTHLY, "d" * 64, 1, 12)
        env.vm.value = 10 * ATTO * 800 // 10000
        with env.vm.expect_revert("Admin cannot hold insurance policies"):
            env.c.create_policy("ADM-01", 10 * ATTO)
        with env.vm.expect_revert("Admin cannot hold insurance policies"):
            env.c.create_policy(PLOT, 10 * ATTO)  # nor an active plot

    def test_staging_delay_is_seven_days(self, env):
        assert env.c.get_config()["staging_delay_seconds"] == 7 * DAY

    def test_exposure_cap_bounds(self, env):
        env.vm.sender = env.admin
        with env.vm.expect_revert("out of bounds"):
            env.c.set_max_epoch_exposure(0)
        with env.vm.expect_revert("out of bounds"):
            env.c.set_max_epoch_exposure(10**30)
        env.c.set_max_epoch_exposure(50 * ATTO)

    # Issues #3/#4: share-based vault ------------------------------------------
    def test_lp_share_fair_loss_distribution(self, env):
        """Two equal LPs. One tries to leave just before a claim is recognised;
        the remaining LP must not be left holding the whole loss."""
        provide(env.c, env.vm, env.admin, 100 * ATTO)  # second LP, equal shares
        buy(env.c, env.vm, env.farmer, 20 * ATTO)  # 1.6 premium, 20 reserved
        env.vm.sender = env.lp
        early_exit = env.c.withdraw_liquidity(100 * ATTO)  # all of bob's shares
        # Pro-rata of FREE capital only: reserved capital stays for everyone.
        assert early_exit == (201_600_000_000_000_000_000 - 20 * ATTO) // 2

        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, TIER3)  # catastrophic: full 20 GEN payout
        env.vm.sender = env.farmer
        assert int(env.c.evaluate_claim("P-1")["approved_payout"]) == 20 * ATTO

        # The LP who stayed absorbed exactly the same per-share outcome.
        stayer = int(env.c.get_lp(_hex(env, env.admin))["value"])
        assert stayer == early_exit

    def test_lp_loss_is_proportional_when_all_stay(self, env):
        provide(env.c, env.vm, env.admin, 300 * ATTO)  # 3:1 split
        pid = buy(env.c, env.vm, env.farmer, 20 * ATTO)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, TIER3)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)
        bob = int(env.c.get_lp(_hex(env, env.lp))["value"])
        alice = int(env.c.get_lp(_hex(env, env.admin))["value"])
        assert abs(alice - 3 * bob) <= 3  # rounding dust only
        assert abs(bob + alice - int(env.c.get_pool()["pool_balance"])) <= 2

    def test_lp_earns_accumulated_premiums(self, env):
        buy(env.c, env.vm, env.farmer, 10 * ATTO)  # 0.8 GEN premium into the pool
        warp_to(env.vm, T0 + timedelta(days=90 + 15))
        env.vm.sender = env.farmer
        env.c.expire_policy("P-1")  # unclaimed: reserve released, premium kept
        assert env.c.get_pool()["share_price_atto"] == str(1008 * 10**15)

        # A later depositor buys in at the higher share price.
        provide(env.c, env.vm, env.admin, 100800 * 10**15)
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

    # Issue #5: payload validation ---------------------------------------------
    def test_validator_rejects_malformed_leader_payload(self, env):
        pid = buy(env.c, env.vm, env.farmer)
        now = eval_time()
        warp_to(env.vm, now)
        uniform(env.vm, now, TIER2)
        env.vm.sender = env.farmer
        env.c.evaluate_claim(pid)

        good = {"tier": 2, "median_deficit": 64, "source_count": 3, "reasoning": "ok"}
        assert env.vm.run_validator(leader_result=good) is True

        bad = {
            "negative deficit": {**good, "median_deficit": -5},
            "deficit over 100": {**good, "median_deficit": 400},
            "tier out of range": {**good, "tier": 9},
            "tier not int": {**good, "tier": "2"},
            "bool tier": {**good, "tier": True},
            "float deficit": {**good, "median_deficit": 64.5},
            "tier/deficit mismatch": {**good, "tier": 3, "median_deficit": 10},
            "settled below quorum": {**good, "source_count": 1},
            "too many sources": {**good, "source_count": 99},
            "oversized reasoning": {**good, "reasoning": "A" * 5000},
            "control chars": {**good, "reasoning": "ok\x00\x1b[31m"},
            "reasoning not str": {**good, "reasoning": {"x": 1}},
            "missing key": {k: v for k, v in good.items() if k != "reasoning"},
            "not a dict": [2, 64, 3, "ok"],
        }
        for name, payload in bad.items():
            assert env.vm.run_validator(leader_result=payload) is False, name

    def test_stored_reasoning_is_canonical_not_leader_text(self, env):
        pid, claim = claim_with(env, TIER2)
        assert claim["reasoning"] == "3 sources agree on tier 2 (SEVERE_DROUGHT), median sustained deficit 64%"
