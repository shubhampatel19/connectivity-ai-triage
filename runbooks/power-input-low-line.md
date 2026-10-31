---
id: power-input-low-line
title: Low AC input voltage at the site
alert_codes: [power.rail_out_of_range]
categories: [input_power]
---
# Low AC input voltage at the site

## Symptoms
The site's AC supply is low. Both power supplies see it, the 12V rail sags as the supplies run short of headroom, and other equipment on the same circuit is usually affected too. It tends to hit many devices at a site at once.

## Reading the evidence
Both PSU1 and PSU2 report their AC input below the 200V low-line threshold (normal is about 230V). Two supplies failing at the same moment is very unlikely; one supply line feeding both is not. There's no predictive failure on either PSU.

## Fix
Contact the site: their electrician should check the circuit, the building supply and any UPS in the path. Don't ship power supplies. Moving the device to a UPS-backed circuit protects it in the meantime.

## Escalate
If the site measures normal mains voltage at the outlet, suspect the PDU between the outlet and the device.
