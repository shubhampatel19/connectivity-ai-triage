---
id: thermal-fan-failure
title: Cooling fan failure
alert_codes: [thermal.critical]
categories: [fan_failure]
---
# Cooling fan failure

## Symptoms
One of the device's two fans stops. The BMC logs the fan's tachometer at 0 RPM, marks it failed and reports fan redundancy lost; the surviving fan runs flat out, and the CPU climbs past its thermal fault threshold and throttles. Usually one device at a time.

## Reading the evidence
The agent's own thermal lines and the alert report `fan=` as the average of both fans, so one dead fan and one at full speed (about 7200 RPM) shows as roughly 3600 RPM, which looks healthy. Don't trust the average: look for the BMC's per-fan readings (`fan status: fan1 ... fan2 ...`) and a `tachometer reads 0 RPM` line. Exhaust runs somewhat hotter than usual above the inlet; the inlet itself is normal.

## Fix
Ship a replacement fan module (FRU part FAN-E1) and schedule the swap; it's hot-swappable. Until then, reduce the device's workload so it stops throttling.

## Escalate
If a new fan also reads 0 RPM, suspect the fan controller on the mainboard and open an RMA for the device.
