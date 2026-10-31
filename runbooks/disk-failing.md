---
id: disk-failing
title: Failing disk
alert_codes: [storage.reallocated_sectors]
categories: [disk_failing]
---
# Failing disk

## Symptoms
The disk is failing now: reads fail, sectors are reallocated in bursts, and more sectors are waiting to be remapped. Data on it is at risk, including the device's buffered telemetry and spooled log bundles.

## Reading the evidence
Kernel `I/O error` lines on the disk, a SMART report with pending sectors above zero, and the RAID controller reporting a predictive failure for the disk. Errors keep coming after the first burst. Percentage used can be anywhere: this is failure, not wear.

## Fix
Urgent. Back up the device configuration, then ship a replacement disk (FRU part SSD-240) for swap within 24 hours. Don't reimage the device to clear the errors: it destroys the evidence and won't fix the media.

## Escalate
Several devices from one shipment failing at once points to a bad disk batch: escalate to supply chain with serial numbers.
