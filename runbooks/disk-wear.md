---
id: disk-wear
title: Disk wear (isolated sector reallocation)
alert_codes: [storage.reallocated_sectors]
categories: [disk_wear]
---
# Disk wear (isolated sector reallocation)

## Symptoms
An ageing disk remaps a sector or two to its spares. That's normal wear: the disk is doing its job, and nothing is lost. The count creeps up slowly over weeks.

## Reading the evidence
The SMART report shows a small reallocated count, zero pending sectors, and a high percentage used (low available spare). There are no kernel I/O errors and no RAID warnings. Pending sectors or read errors would mean the disk is failing now; see disk-failing.

## Fix
Not urgent. Schedule a disk replacement (FRU part SSD-240) at the site's next maintenance window, sooner once available spare falls below 5%. Keep the case open to watch the count.

## Escalate
If the count starts rising daily, or pending sectors appear, treat it as a failing disk.
