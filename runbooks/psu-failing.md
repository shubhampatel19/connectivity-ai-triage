---
id: psu-failing
title: Failing power supply (PSU1)
alert_codes: [power.rail_out_of_range]
categories: [psu_failing]
---
# Failing power supply (PSU1)

## Symptoms
PSU1 is wearing out: its 12V rail sags toward 11.8V and flaps in and out of tolerance, and the BMC asserts a predictive failure for it, degrading power redundancy.

## Reading the evidence
The PSU's own diagnostics are the deciding evidence: a `PSU1 predictive failure asserted` line, and output ripple above its 120mV limit. The AC input is normal (about 230V), so the supply isn't being starved; it's failing on its own. A rail sag with low AC input on both PSUs is a site power problem instead, and a PSU1 input that drops to nothing is a cable.

## Fix
Move the load to PSU2 and ship a replacement PSU (FRU part PSU-650); PSUs are hot-swappable while the other one carries the load.

## Escalate
If a new PSU also asserts a predictive failure, escalate to hardware engineering: the backplane may be at fault.
