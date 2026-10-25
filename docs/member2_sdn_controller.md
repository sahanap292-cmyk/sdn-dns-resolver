# Member 2: Mininet Topology and SDN Controller

## 1. Role in the system
The controller makes the network react to the DNS application. Name servers send
UDP heartbeats, the controller keeps a health table, and flow rules redirect
client queries (sent to a virtual resolver IP) to a healthy, least-loaded server.

## 2. Topology (topology/topo.py)
    h1 h2 h3 h4 -- s1 -- s2 -- s3 -- ns1 ns2 ns3
                          |
                          r1

| Node | IP | MAC |
|---|---|---|
| h1-h4 (clients) | 10.0.0.1-4 | 00:00:00:00:00:01-04 |
| r1 (resolver) | 10.0.0.10 | 00:00:00:00:00:0a |
| ns1, ns2, ns3 (name servers) | 10.0.0.21, .22, .23 | 00:00:00:00:00:15, 16, 17 |
| Virtual resolver | 10.0.0.100 | 00:00:00:00:00:64 (made up by controller) |
| Virtual controller | 10.0.0.254 | 00:00:00:00:00:fe (made up by controller) |

Three OpenFlow 1.3 switches, remote Ryu controller at 127.0.0.1:6653.

## 3. Controller (controller/dns_controller.py)

### Flow table design
| Priority | Match | Action | Purpose |
|---|---|---|---|
| 0 | anything | to controller | table-miss |
| 1 | in_port, eth_src, eth_dst | forward | learned L2 forwarding |
| 5 | UDP dst 5353 | to controller | controller sees every DNS flow |
| 8 | UDP dst 9999 to 10.0.0.254 | to controller | heartbeats |
| 10 | UDP 5353, src IP, dst IP | forward | direct DNS flows |
| 20 | UDP 5353 to 10.0.0.100 (and reverse) | rewrite IP/MAC, forward | redirection |
| 30 | UDP 5353 from a flooding client | drop | blocking |

### Dynamic behaviour
1. **Heartbeat monitoring:** servers send JSON `{"type":"HB","server":"ns1","load":12}`
   every 2 s to 10.0.0.254:9999. The switch hands it to the controller (packet-in),
   which identifies the server by source IP and updates a health table
   (alive, load, last_seen).
2. **Failure detection:** no heartbeat for 6 s marks a server DOWN and deletes
   its redirect rules on all switches (rules are tagged with a per-server cookie).
3. **Redirection:** a query to 10.0.0.100 is rewritten (dst IP and MAC) to the
   chosen server, and a reverse rule rewrites reply sources back to 10.0.0.100.
4. **Load balancing:** least-loaded healthy server (ties rotate);
   `POLICY=round_robin` switches to round-robin.
5. **Recovery:** when a DOWN server's heartbeat returns, it is logged as
   RECOVERED and rejoins the rotation.
6. **Flood blocking:** every 2 s the controller reads flow statistics from s1.
   A client above 200 queries/s gets a drop rule for 30 s, then it is unblocked.
7. **Static baseline:** `MODE=static` maps clients to servers by a fixed rule and
   ignores health, load and floods. Used for the performance comparison.

### Configuration (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| MODE | sdn | sdn or static |
| POLICY | least_loaded | least_loaded or round_robin |
| FLOOD_RATE | 200 | queries/s per client before blocking |
| BLOCK_SECS | 30 | block duration |

## 4. How to run
Three terminals, started in this order.

    # Terminal 1: cleanup, then controller
    sudo mn -c
    source ~/ryu-env/bin/activate
    cd ~/CN
    ryu-manager controller/dns_controller.py              # SDN mode
    MODE=static ryu-manager controller/dns_controller.py  # baseline

    # Terminal 2: topology (never run "mn -c" while the controller runs)
    cd ~/CN
    sudo python3 topology/topo.py

    # At the mininet> prompt: start heartbeats and echo servers
    source /home/sahana/CN/tests/start_services.txt

## 5. Tests performed
| Test | Action | Result |
|---|---|---|
| Connectivity | pingall | 0% dropped (56/56) |
| Redirection | 4 clients query 10.0.0.100 | Replies come from real servers, source shown as 10.0.0.100 |
| Failover | stop one server's heartbeat | Marked DOWN after 6 s, rules deleted, next query served by another server |
| Least-loaded | raise ns1 load to 50 | New queries go to ns2 (load 10) |
| Recovery | restart heartbeat | RECOVERED logged, server serves queries again |
| Flood | 60000 packets in 8 s from h1 | h1 blocked for 30 s, h2 unaffected, then unblocked |
| Static baseline | kill ns1 | Queries still go to the dead server and time out |

## 6. Event log
controller_events.log holds timestamped decisions (UP, DOWN, RECOVERED, REDIRECT,
FLOOD DETECTED, UNBLOCKED). Failover time = gap between "marked DOWN" and the first
REDIRECT to another server for the same client.

## 7. Limitations
- The port map is static (fixed topology) instead of discovered with LLDP.
- Flood detection reads only s1, where the clients are attached.
- Detection takes up to 6 s (3 missed heartbeats), a trade-off against false alarms.
- A single controller is a single point of failure.
