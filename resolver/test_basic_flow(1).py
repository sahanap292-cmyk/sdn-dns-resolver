"""
test_basic_flow.py - unit/integration tests for the basic flow. NO Mininet or Ryu needed.

Run:
    python3 test_basic_flow.py -v
"""
import time
import unittest

from common import ZONE
from health import HealthTable
from nameserver import NameServer
from resolver import Resolver, TTLCache


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestTTLCache(unittest.TestCase):
    def test_hit_then_expiry(self):
        clk = FakeClock()
        c = TTLCache(clk)
        c.put("a.sdn", "1.1.1.1", 5)
        self.assertEqual(c.get("a.sdn")[0], "1.1.1.1")
        clk.t += 4.9
        self.assertIsNotNone(c.get("a.sdn"))
        clk.t += 0.2
        self.assertIsNone(c.get("a.sdn"))

    def test_zero_ttl_not_cached(self):
        c = TTLCache(FakeClock())
        c.put("a.sdn", "1.1.1.1", 0)
        self.assertIsNone(c.get("a.sdn"))


class TestHealthTable(unittest.TestCase):
    def test_up_then_timeout_down(self):
        clk = FakeClock()
        h = HealthTable(timeout=1.5, clock=clk)
        h.heartbeat("ns1", "10.0.0.11", 5301, 0)
        self.assertTrue(h.status()["ns1"]["up"])
        clk.t += 1.0
        h.check_timeouts()
        self.assertTrue(h.status()["ns1"]["up"])
        clk.t += 1.0
        h.check_timeouts()
        self.assertFalse(h.status()["ns1"]["up"])
        self.assertEqual([e["event"] for e in h.events], ["up", "down"])

    def test_graceful_stop_is_immediate(self):
        h = HealthTable(clock=FakeClock())
        h.heartbeat("ns1", "10.0.0.11", 5301, 0)
        h.event("ns1", "stopping")
        self.assertFalse(h.status()["ns1"]["up"])

    def test_loss_estimate(self):
        h = HealthTable(clock=FakeClock(), window=20)
        for seq in range(20):
            if seq % 4 != 0:                     # drop every 4th heartbeat = 25% loss
                h.heartbeat("ns1", "10.0.0.11", 5301, seq)
        h.heartbeat("ns1", "10.0.0.11", 5301, 20)
        self.assertAlmostEqual(h.status()["ns1"]["loss"], 0.2, places=2)

    def test_restart_resets_sequence(self):
        h = HealthTable(clock=FakeClock())
        for seq in range(10):
            h.heartbeat("ns1", "10.0.0.11", 5301, seq)
        h.heartbeat("ns1", "10.0.0.11", 5301, 0)   # restarted server
        self.assertEqual(h.status()["ns1"]["loss"], 0.0)


class TestNameServerLookup(unittest.TestCase):
    def test_lookup(self):
        ns = NameServer("ns1")
        r = ns.lookup("WWW.example.sdn.")
        self.assertEqual((r["rcode"], r["ip"]), ("OK", ZONE["www.example.sdn"][0]))
        self.assertEqual(ns.lookup("nope.example.sdn")["rcode"], "NXDOMAIN")


class TestResolverFlow(unittest.TestCase):
    """Real UDP on localhost: resolver -> name servers."""

    def setUp(self):
        self.ns1 = NameServer("ns1", "127.0.0.1", 0)
        self.ns2 = NameServer("ns2", "127.0.0.1", 0)
        self.ns1.start()
        self.ns2.start()
        self.ups = [("ns1", "127.0.0.1", self.ns1.port), ("ns2", "127.0.0.1", self.ns2.port)]

    def tearDown(self):
        self.ns1.stop(False)
        self.ns2.stop(False)

    def test_resolve_and_cache(self):
        r = Resolver(self.ups, timeout=0.3)
        a = r.resolve("www.example.sdn")
        self.assertEqual((a["rcode"], a["ip"], a["source"]), ("OK", "10.1.0.10", "ns1"))
        b = r.resolve("www.example.sdn")
        self.assertEqual(b["source"], "cache")
        s = r.stats_snapshot()
        self.assertEqual((s["queries"], s["cache_hits"], s["cache_misses"]), (2, 1, 1))

    def test_nxdomain_not_cached(self):
        r = Resolver(self.ups, timeout=0.3)
        self.assertEqual(r.resolve("nope.example.sdn")["rcode"], "NXDOMAIN")
        self.assertEqual(r.resolve("nope.example.sdn")["source"], "ns1")

    def test_cache_expires(self):
        r = Resolver(self.ups, timeout=0.3)
        r.resolve("short.example.sdn")                 # ttl = 2 s
        time.sleep(2.2)
        self.assertNotEqual(r.resolve("short.example.sdn")["source"], "cache")

    def test_static_failover_to_second_server(self):
        r = Resolver(self.ups, timeout=0.2, use_cache=False)
        self.ns1.stop(False)                           # ns1 dies
        a = r.resolve("www.example.sdn")
        self.assertEqual((a["rcode"], a["source"]), ("OK", "ns2"))
        self.assertEqual(r.stats_snapshot()["upstream_timeouts"], 1)

    def test_all_down_gives_servfail(self):
        r = Resolver(self.ups, timeout=0.2, use_cache=False)
        self.ns1.stop(False)
        self.ns2.stop(False)
        self.assertEqual(r.resolve("www.example.sdn")["rcode"], "SERVFAIL")

    def test_sdn_order_skips_down_and_degraded(self):
        ups = [("ns1", "1", 1), ("ns2", "2", 2), ("ns3", "3", 3)]
        r = Resolver(ups, mode="sdn", controller=("x", 1))
        r._status = {"ns1": {"up": False, "loss": 0.0},
                     "ns2": {"up": True, "loss": 0.4},
                     "ns3": {"up": True, "loss": 0.0}}
        self.assertEqual([u[0] for u in r.upstream_order()], ["ns3", "ns2", "ns1"])

    def test_sdn_without_status_falls_back_to_static(self):
        ups = [("ns1", "1", 1), ("ns2", "2", 2)]
        r = Resolver(ups, mode="sdn", controller=("x", 1))
        self.assertEqual([u[0] for u in r.upstream_order()], ["ns1", "ns2"])


if __name__ == "__main__":
    unittest.main()
