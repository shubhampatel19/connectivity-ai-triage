---
id: thermal-sensor-fault
title: Faulty temperature sensor
alert_codes: [thermal.critical]
categories: []
---
# Faulty temperature sensor

## Symptoms
Temperature readings jump between impossible values, for example from 50°C to 127°C and back in one sample, or stay at exactly the same value for hours. Fans don't react the way they would to real heat, and the device doesn't throttle.

## Reading the evidence
Single-sample spikes with no ramp, values outside the sensor's range, and no change in fan duty. A real overheat climbs over a minute or more and the fans follow it up.

## Fix
Update the firmware, which recalibrates the sensor driver; if readings stay implausible, mark the alert as a false positive and schedule a mainboard replacement.
