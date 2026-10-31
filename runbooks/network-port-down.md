---
id: network-port-down
title: Network port link down
alert_codes: []
categories: []
---
# Network port link down

## Symptoms
The kernel logs a network port's link going down. On devices where the port isn't cabled (eth1 on most installs), this repeats harmlessly. On the uplink port (eth0), the device loses connectivity.

## Fix
Ignore `eth1: link down (port not cabled)`. For eth0, check the cable and the switch port at the site.
