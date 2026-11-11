# Confirmed OctoPrint printer transport — 2026-11-11

The operator explicitly confirmed that the working OctoPrint session shown in the supplied screenshots uses the **Remote Connection plugin in Raw TCP mode to the Neptune 2 MKS WiFi module on port 8080**. OctoPrint reports `Operational` and receives repeated temperature responses for tool and bed.

## Verified

- OctoPrint web UI accessible over the local network.
- OctoPrint connected to the printer using **Raw TCP**, **MKS WiFi port 8080**, not USB or RFC2217.
- Printer status is `Operational` and live temperature telemetry arrives.

## Not yet verified

- Sustained full-file G-code streaming and print completion.
- Every machine control command, file transfer, reconnect behavior, or edge case.

## Engineering consequences

- Implement the Observatory-to-OctoPrint adapter now; do not block it on another basic MKS protocol discovery phase.
- In OctoPrint mode, OctoPrint alone must own the MKS TCP connection. Disable Observatory's direct MKS poller, command executor transport, SD polling, and direct upload path.
- Preserve existing visual dashboard, Three.js Digital Twin, authentication and operator safeguards.
- Keep OctoPrint's authenticated API key server-side and preserve its persistent volume and current Remote Connection settings.
- No printer commands or live configuration changes are authorized by this confirmation.
