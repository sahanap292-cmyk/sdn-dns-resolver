# SDN DNS-like Resolution - Resolver, Integration and Evaluation

## 1. Architecture


```
   h1    h2    h3    h4            clients  (dns_client.py)
     \    |     |    /
          [  s1  ]
             |
 r1 --------[  s2  ]-------- root namespace 10.0.0.254
 resolver.py   |             Ryu + sdn_controller.py
 (TTL cache)   |                  ^        ^
             [  s3  ]             | UDP 9999: heartbeats, events
             /  |  \              | (travel through the switches)
          ns1  ns2  ns3 ----------+
          nameserver.py
```

**Components**

- `nameserver.py` - authoritative server for the zone; sends a UDP heartbeat every 0.5s and `started`/`stopping` events to the controller.
- `sdn_controller.py` - Ryu app: learning switch + health service. Marks a server DOWN after 1.5s without heartbeats (or at once on `stopping`) and estimates per-server heartbeat loss over the last 20 heartbeats.
- `resolver.py` - TTL cache in front of the name servers. In `sdn` mode it polls the controller every 0.5s, skips servers marked DOWN and tries lossy servers last; in `static` mode it uses a fixed order and only reacts to timeouts.
- `dns_client.py` - query generator used on h1-h4.

**Normal flow:** client -> resolver -> (cache hit: answer) or (miss: ask first healthy name server, store answer for its TTL) -> client.

**Failure flow:** name server dies -> heartbeats stop -> controller marks it DOWN -> resolver sees the change on its next poll -> queries go straight to a healthy server.

## 2. Test cases - basic flow

| Test | Result | Detail |
|---|---|---|
| controller receives heartbeats from ns1, ns2, ns3 | PASS | {'ns1': True, 'ns2': True, 'ns3': True} |
| every name in the zone resolves to the right IP | PASS |  |
| unknown name returns NXDOMAIN | PASS | NXDOMAIN |
| second lookup is served from the TTL cache | PASS | sources=['ns1', 'cache'] |
| cache entry expires after its TTL (2s) | PASS | sources=['ns1', 'ns1'] |
| all four clients (h1-h4) can resolve through r1 | PASS |  |

## 3. Performance results

### 3.1 Cache hit rate and latency (300 queries, 5 names, TTL 3s)

| Mode | Hit rate | Mean (hit) ms | Mean (miss) ms | p50 ms | p95 ms | Success |
|---|---|---|---|---|---|---|
| static | 0.95 | 2.3 | 3.38 | 1.88 | 6.26 | 100.0% |
| sdn | 0.95 | 2.55 | 2.81 | 1.9 | 6.29 | 100.0% |

### 3.2 Latency without cache (healthy network, 200 queries)

| Mode | Mean ms | p50 ms | p95 ms | p99 ms | Success |
|---|---|---|---|---|---|
| static | 2.2 | 1.8 | 4.93 | 8.75 | 100.0% |
| sdn | 1.87 | 1.61 | 3.13 | 7.62 | 100.0% |

### 3.3 Failover (ns1 killed with SIGKILL 2s into a 10s probe, no cache)

| Mode | Controller detected after (s) | Recovered to normal latency after (s) | Failed queries | Slow queries | Worst latency ms |
|---|---|---|---|---|---|
| static | 1.28 | never (within 10s probe) | 0 | 16 / 16 | 555.73 |
| sdn | 1.19 | 1.64 | 0 | 3 / 66 | 519.5 |

### 3.4 Packet loss on ns1 (tc netem, 15s probe after 12s warm-up, no cache)

| Mode | netem loss | Controller's loss estimate | Answers from ns1 | Mean ms | p95 ms | p99 ms | Success |
|---|---|---|---|---|---|---|---|

## 4. Observations (generated from the numbers above)

- Failover: the controller noticed the dead server after 1.19s. The SDN resolver returned to normal latency after 1.64s; the static resolver did not return to normal latency during the probe (16 slow queries vs 3).
- Cache: hit rate was 0.95; hits averaged 2.3 ms versus 3.38 ms for misses.

## 5. Limitations

- Everything runs in one Mininet VM, so latencies are small and reflect software overhead more than real network delay.
- Each experiment is a single run; repeat it a few times and average before quoting numbers.
- Detection time is bounded by the settings in `common.py` (heartbeat 0.5s, timeout 1.5s, resolver poll 0.5s).
- The static baseline is a resolver with a fixed server order and a 0.5s timeout; a baseline with smarter retry logic would narrow the gap.
