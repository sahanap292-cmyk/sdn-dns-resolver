#!/bin/bash
python3 /home/sahana/CN/tests/flood.py 10.0.0.100 6
python3 /home/sahana/CN/tests/udp_client.py 10.0.0.100
