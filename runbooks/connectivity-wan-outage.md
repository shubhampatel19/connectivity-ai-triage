---
id: connectivity-wan-outage
title: Site WAN outage
alert_codes: []
categories: []
---
# Site WAN outage

## Symptoms
Devices at a site can't reach the cloud: their logs fill with `telemetry upload failed` warnings and a growing count of buffered samples, and the site gateway's queue grows. Nothing is wrong with the devices themselves.

## Fix
Contact the site's network team or ISP. Don't ship hardware. After the link returns, check the gateway queue drains and the site's telemetry has no gaps.
