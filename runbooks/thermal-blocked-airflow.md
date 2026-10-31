---
id: thermal-blocked-airflow
title: Overheating from blocked airflow
alert_codes: [thermal.critical]
categories: [blocked_airflow]
---
# Overheating from blocked airflow

## Symptoms
The CPU overheats although both fans run at full duty and the air coming in is at normal room temperature. The fans are spinning but little air moves through the chassis: a clogged intake filter, a blocked vent, cables packed against the intake, or a device installed in a closed cabinet.

## Reading the evidence
Compare inlet and exhaust in the BMC's `fan status` lines. Air normally leaves about 10 to 12°C warmer than it enters. With fans at 100% duty and a normal inlet (around 24°C), an exhaust 25 to 30°C above the inlet means the air isn't getting through. Both fans report thousands of RPM: this is not a fan failure. The inlet stays under its operating limit: this is not a hot room.

## Fix
Ask the site to clean or replace the intake filter, clear the vents, and give the device the clearance its installation guide asks for. No parts need to ship.

## Escalate
If the exhaust rise stays high with a clean intake and open vents, escalate to hardware engineering with the BMC readings.
