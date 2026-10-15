# Wi-Fi provisioning research (open)

**Status:** not started. Nothing in the app depends on this.

## Goal
Put factory-reset Jebao MD-4.4 / Doser 2.4 pumps on a Wi-Fi network without the phone app, so many pumps can be set up at once (for example 50+ pumps for solid-phase extraction rigs). The result would be a small command-line script, `jebao_provisioner.py`.

## What we know
- Once a pump is on Wi-Fi it is bound to a Jebao (Gizwits) cloud account and only accepts control through the cloud. That is why the app uses the Gizwits API instead of the local TCP/UDP protocol used by `python-jebao`.
- Binding a new pump to Wi-Fi is done today with the Jebao Aqua app.
- These pumps likely use an Espressif chip (ESP8266/ESP32), so provisioning is probably **ESPTouch / SmartConfig** or a short-lived access-point mode.

## Plan
1. **Capture.** Run Wireshark on the Mac's Wi-Fi interface (usually `en0`) while the Jebao Aqua app (iOS app on Apple silicon) adds a pump that is in pairing mode (hold the Wi-Fi button until it flashes). Stop when the pump's light turns solid.
2. **Identify the method.**
   - A flood of UDP packets of varying lengths to `255.255.255.255` or multicast addresses means SmartConfig/ESPTouch. An existing open-source ESPTouch implementation can probably be reused.
   - A connection to a temporary `Jebao_…` network and traffic to its gateway (often `192.168.4.1`) means access-point mode. Look for the JSON or binary payload carrying the SSID and password.
3. **Reproduce.** Write `jebao_provisioner.py` with the standard `socket` library to send the same payload.
4. **Bind.** Check whether a newly provisioned pump still needs binding to the cloud account (the Gizwits API has a bind endpoint) before the app can see it.

## Notes
- Don't commit packet captures. They contain your Wi-Fi password.
- Reference material (manual, decompiled app, third-party code) is kept outside this public repo.
