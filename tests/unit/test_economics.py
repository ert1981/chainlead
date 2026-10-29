import threading

import pytest

from chainlead.config import Settings
from chainlead.economics import BudgetExceeded, ProviderError, ProviderRouter
from chainlead.providers.base import Provider
from chainlead.storage import Storage


class Paid(Provider):
    def __init__(self, name, cost, caps=("contacts",)):
        self.name, self.costs, self.capabilities = name, {"contacts": cost}, frozenset(caps)


@pytest.fixture
def router():
    st = Storage(":memory:")
    st.ensure_budget("t", "c", 0.05)
    return ProviderRouter(st, Settings(db_path=":memory:"), "t", "c",
                          [Paid("pricey", 0.02), Paid("free", 0.0), Paid("cheap", 0.01)])


def test_free_sources_preferred(router):
    assert [p.name for p in router.available("contacts")] == ["free", "cheap", "pricey"]
    assert [p.name for p in router.available("contacts", ["pricey", "cheap"])] == ["cheap", "pricey"]


def test_budget_hard_stop_cache_and_refund(router):
    p = router.providers["pricey"]
    assert router.call(p, "contacts", {"x": 1}, lambda: {"ok": 1}) == {"ok": 1}
    assert router.call(p, "contacts", {"x": 1}, lambda: {"ok": 2}) == {"ok": 1}      # cached -> no charge
    router.call(p, "contacts", {"x": 2}, lambda: {"ok": 3})
    with pytest.raises(BudgetExceeded):
        router.call(p, "contacts", {"x": 3}, lambda: {"ok": 4})                        # 0.06 > 0.05
    with pytest.raises(ProviderError):
        router.call(router.providers["cheap"], "contacts", {"x": 4}, lambda: 1 / 0)    # refunded
    s = router.summary()
    assert s["spent_usd"] == pytest.approx(0.04)
    assert s["by_provider"]["pricey"] == {"calls": 4, "cached": 1, "errors": 0, "skipped_budget": 1, "cost_usd": 0.04}
    assert s["by_provider"]["cheap"]["errors"] == 1 and s["by_provider"]["cheap"]["cost_usd"] == 0


def test_budget_is_atomic_under_concurrency():
    st = Storage(":memory:")
    st.ensure_budget("t", "c", 1.0)
    ok = []

    def spend():
        for _ in range(50):
            ok.append(st.try_spend("t", "c", 0.01))

    ts = [threading.Thread(target=spend) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(ok) == 100 and st.budget("t", "c")["spent_usd"] == pytest.approx(1.0)
