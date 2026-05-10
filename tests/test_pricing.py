import math
from src.pricing import (
    bs_price, iron_condor_credit, iron_condor_settlement, round_to_grid,
)


def test_bs_call_atm_positive():
    p = bs_price(S=100, K=100, T=1/252, r=0.045, sigma=0.20, kind="c")
    assert p > 0


def test_bs_put_call_parity():
    """C - P = S - K e^(-rT)"""
    S, K, T, r, sigma = 100.0, 105.0, 30/365, 0.04, 0.18
    c = bs_price(S, K, T, r, sigma, "c")
    p = bs_price(S, K, T, r, sigma, "p")
    rhs = S - K * math.exp(-r * T)
    assert abs((c - p) - rhs) < 1e-3


def test_bs_zero_T_intrinsic():
    assert bs_price(110, 100, 0, 0.04, 0.2, "c") == 10
    assert bs_price(90,  100, 0, 0.04, 0.2, "p") == 10
    assert bs_price(110, 100, 0, 0.04, 0.2, "p") == 0


def test_iron_condor_credit_basic():
    # Sell put 1.0, buy put 0.4 -> 0.6 ; Sell call 0.9, buy call 0.3 -> 0.6 ; total 1.20
    cred = iron_condor_credit(1.0, 0.4, 0.9, 0.3)
    assert abs(cred - 1.20) < 1e-9


def test_iron_condor_settlement_inside_zone():
    # Close inside the win zone -> both spreads worthless -> 0
    s = iron_condor_settlement(Sc=5800, Kp=5750, Lp=5725, Kc=5850, Lc=5875)
    assert s == 0


def test_iron_condor_settlement_put_breach_partial():
    # Close between Lp and Kp -> partial loss equal to Kp - Sc
    s = iron_condor_settlement(Sc=5740, Kp=5750, Lp=5725, Kc=5850, Lc=5875)
    assert s == 10


def test_iron_condor_settlement_put_max_loss():
    # Close below Lp -> max put-side loss = wing width
    s = iron_condor_settlement(Sc=5700, Kp=5750, Lp=5725, Kc=5850, Lc=5875)
    assert s == 25


def test_iron_condor_settlement_call_max_loss():
    s = iron_condor_settlement(Sc=5900, Kp=5750, Lp=5725, Kc=5850, Lc=5875)
    assert s == 25


def test_round_to_grid():
    assert round_to_grid(5172.3, 5) == 5170
    assert round_to_grid(5172.6, 5) == 5175
    assert round_to_grid(7388.0, 25) == 7400
