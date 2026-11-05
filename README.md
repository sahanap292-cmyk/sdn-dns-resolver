# Distributed DNS-Like Name Resolution System (UDP + SDN)

Computer Networks (UE25CS243A), Section G
Team: Sahana Patil, Ronit Duggal, Priyadharshan B

## What it does
A DNS-like service over UDP. Clients ask a caching resolver for a name. On a cache miss
the resolver queries a virtual address (10.0.0.100); the Ryu controller redirects that
query to a healthy, least-loaded name server by rewriting IP and MAC, and rewrites the
reply back. Name servers send UDP heartbeats to the controller, which detects failures,
recovers servers, and blocks flooding clients.

## Layout
    app/         UDP name servers, client, message format (dns_proto.py), records
    resolver/    caching resolver, health service, evaluation scripts
    controller/  Ryu controller (dns_controller.py)
    topology/    Mininet topology (topo.py)
    tests/       start files, flood and stats helpers
    docs/        controller documentation
    results/     controller event logs

## Requirements
Ubuntu VM with Mininet, and Ryu 4.34 in a Python 3.9 virtual environment (~/ryu-env).

## Run (three terminals, in this order)
    sudo mn -c
    source ~/ryu-env/bin/activate && cd ~/CN && ryu-manager controller/dns_controller.py
    cd ~/CN && sudo python3 topology/topo.py
At the mininet> prompt:
    source /home/sahana/CN/tests/start_real.txt
    source /home/sahana/CN/tests/start_resolver.txt
    h1 python3 /home/sahana/CN/app/client.py -s 10.0.0.10:5353 www.example.com
Baseline for comparison: start the controller with MODE=static.
Details of the controller: docs/member2_sdn_controller.md

## Evaluation
    cd resolver && ryu-manager sdn_controller.py        (ryu environment)
    cd resolver && sudo python3 run_eval.py             (second terminal)
Results are written to resolver/results/.
