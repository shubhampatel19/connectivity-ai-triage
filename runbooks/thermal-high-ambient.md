---
id: thermal-high-ambient
title: Room too hot (high ambient temperature)
alert_codes: [thermal.critical]
categories: [high_ambient]
---
# Room too hot (high ambient temperature)

## Symptoms
The air around the device is too hot: the site's cooling has failed or been switched off, a server room door was left open on a hot day, or the device sits next to another heat source. It usually hits every device at the site at once, and the case service opens one site case for the storm.

## Reading the evidence
The BMC reports the inlet temperature above the 35°C operating limit. Fans run at full duty and the gap between inlet and exhaust stays normal (about 10 to 12°C): the device is cooling as designed, the air it's cooling with is just too warm. Several devices at one site with the same pattern confirms it.

## Fix
Call the site's facilities contact: restoring room cooling fixes every device at once. Don't ship fans or devices. If cooling can't be restored soon, ask the site to shut down non-essential equipment to cut the heat load.

## Escalate
If the site reports a normal room temperature while inlets read hot, suspect the inlet sensors or a firmware fault and escalate to engineering.
