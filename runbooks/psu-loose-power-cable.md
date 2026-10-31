---
id: psu-loose-power-cable
title: Loose power cable on PSU1
alert_codes: [power.rail_out_of_range]
categories: [loose_power_cable]
---
# Loose power cable on PSU1

## Symptoms
PSU1's input drops out completely and comes back, again and again: its power cable is loose, its retention clip is missing, or the cable is damaged. Each time, PSU2 picks up the whole load and the 12V rail dips while it does.

## Reading the evidence
`PSU1 AC input lost` lines, each followed by `PSU1 AC input restored`, with PSU2 reporting nothing wrong. The input drops to nothing rather than running low: a low-line reading on both supplies would be a site power problem, and a predictive failure would be the PSU itself.

## Fix
Ask the site to reseat PSU1's power cable at both ends and fit the retention clip; replace the cable if it's damaged. No parts beyond a cable need to ship.

## Escalate
If the input keeps dropping with a new, clipped cable, the PSU's inlet connector is suspect: replace PSU1.
