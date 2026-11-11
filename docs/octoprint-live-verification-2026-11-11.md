## User-supplied live verification (2026-11-11)

Screenshots of the running OctoPrint 1.11.8 instance at the user's private LAN endpoint `192.168.0.9:5001` show:
- Printer status `Operational`.
- Live repeated terminal responses `T:24 /0 B:24 /0` (approximate), consistent with actual tool/bed telemetry; the dashboard reports ~24°C tool and ~23°C bed.
- Remote Connection plugin UI installed and offering Raw/RFC2217 transport options.
- Browser accessibility from a separate iPhone.

**Evidence classification:** OctoPrint is communicating with a printer and receiving live temperatures. Screenshots do not fully show the selected remote hostname, port, or connection type, nor prove a complete print or command/ack stability over time. The MKS raw TCP route is now *user-reported verified for connection/telemetry*, rather than wholly untested, pending direct confirmation of selected endpoint and controlled printing. No physical commands are authorized by this evidence.

**Implementation consequence:** Prioritize the OctoPrint API adapter immediately; do not block software development on another raw TCP handshake. Keep transport selection explicit and fail closed on competing client detection. Do not include the user's private LAN endpoint in production defaults.
