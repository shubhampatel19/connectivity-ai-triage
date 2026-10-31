---
id: certificate-expiry
title: Device certificate nearing expiry
alert_codes: []
categories: []
---
# Device certificate nearing expiry

## Symptoms
The device logs that its certificate expires in some days. Routine: devices renew their certificate automatically a week before expiry.

## Fix
Nothing unless the warning continues inside the last seven days, which means renewal is failing: check the device can reach the enrollment endpoint.
