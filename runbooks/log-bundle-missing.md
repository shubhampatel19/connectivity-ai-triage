---
id: log-bundle-missing
title: Not enough evidence in the case
alert_codes: [thermal.critical, power.rail_out_of_range, storage.reallocated_sectors]
categories: [insufficient_evidence]
---
# Not enough evidence in the case

## Symptoms
A case has no log bundle, or its logs don't cover the fault: the device was within its bundle cooldown, the bundle was lost, or a log storm pushed the evidence out of the device's log ring before it was collected.

## Fix
Request a manual log collection from the device, then triage again. Don't guess a cause from the alert alone when the alert can't tell the causes apart: a thermal alert's averaged fan speed, for example, can hide a dead fan.
