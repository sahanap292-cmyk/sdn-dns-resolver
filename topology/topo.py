#!/usr/bin/env python3
r"""
Mininet topology for the SDN DNS-like resolution project.

          h1 h2 h3 h4            r1             ns1 ns2 ns3
            \  |  |  /            |               \   |   /
                s1 ------------- s2 ------------- s3

Run (controller must already be running):
    sudo python3 topo.py
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=False, autoStaticArp=False)

    info('*** Adding remote controller\n')
    net.addController('c0', controller=RemoteController,
                      ip='127.0.0.1', port=6653)

    info('*** Adding switches (OpenFlow 1.3)\n')
    s1 = net.addSwitch('s1', protocols='OpenFlow13')  # client side
    s2 = net.addSwitch('s2', protocols='OpenFlow13')  # core / resolver
    s3 = net.addSwitch('s3', protocols='OpenFlow13')  # name-server side

    info('*** Adding hosts (fixed IPs and MACs, easy to rewrite later)\n')
    clients = []
    for i in range(1, 5):
        clients.append(net.addHost(f'h{i}', ip=f'10.0.0.{i}/24',
                                   mac=f'00:00:00:00:00:{i:02x}'))
    r1 = net.addHost('r1', ip='10.0.0.10/24', mac='00:00:00:00:00:0a')
    ns1 = net.addHost('ns1', ip='10.0.0.21/24', mac='00:00:00:00:00:15')
    ns2 = net.addHost('ns2', ip='10.0.0.22/24', mac='00:00:00:00:00:16')
    ns3 = net.addHost('ns3', ip='10.0.0.23/24', mac='00:00:00:00:00:17')

    info('*** Adding links\n')
    for h in clients:
        net.addLink(h, s1)
    net.addLink(r1, s2)
    for ns in (ns1, ns2, ns3):
        net.addLink(ns, s3)
    net.addLink(s1, s2)
    net.addLink(s2, s3)

    info('*** Starting network\n')
    net.start()
    info('*** Try: pingall | h1 ping -c2 ns1 | links | dump\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    build()
