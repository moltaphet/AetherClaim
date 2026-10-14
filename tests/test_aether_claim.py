"""AetherClaim: lifecycle + adversarial direct-mode suite."""

import pytest

from conftest import (
    ATTO, CONTRACT, DAY, NORMAL, PLOT, T0, TIER1, TIER2, TIER3,
    buy, eval_time, mock_sources, provide, uniform, warp_to,
)
from datetime import timedelta


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

    def test_stage_then_activate_after_delay(self, env):
        env.vm.sender = env.admin
        env.c.stage_plot("NEW-01", "Test Orchard", "PECAN", 300000, -1000000, 20, "a" * 64, "Mar-Oct")
        assert env.c.get_plot("NEW-01")["active"] is False
        with env.vm.expect_revert("TIMELOCK"):
            env.c.activate_plot("NEW-01")
        warp_to(env.vm, T0 + timedelta(days=1, minutes=1))
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
        env.c.stage_plot("NEW-02", "Staged", "OLIVE", 300000, -50000, 20, "b" * 64, "x")
        env.vm.sender = env.farmer
        env.vm.value = 10**17
        with env.vm.expect_revert("not active"):
            env.c.create_policy("NEW-02", 10**18)

    def test_only_admin_registers_plots(self, env):
        env.vm.sender = env.farmer
        with env.vm.expect_revert("only admin"):
            env.c.stage_plot("EVIL", "x", "x", 0, 0, 10, "c" * 64, "x")

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
        with env.vm.expect_revert("reserved"):
            env.c.withdraw_liquidity(100 * ATTO)
        env.c.withdraw_liquidity(50 * ATTO)

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
