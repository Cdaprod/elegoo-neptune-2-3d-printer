# iPhone Share Sheet → CDA Neptune PrintBay

**This is not native AirDrop.** A Bluetooth name or BLE advertisement does not
make Linux appear in Apple's AirDrop device picker. Native AirDrop requires a
compatible discovery/transfer stack that has not been validated on this Pi.
Do not advertise iOS AirDrop support until actual iPhone discovery and transfer
are demonstrated on the target hardware.

## Architecture

iOS Files / CAD application's Share action → configured iOS Shortcut → HTTPS
reverse proxy (LAN, authenticated, TLS certificate trusted by iOS) →
`ios_share.py` (Pi loopback :8092) → existing authenticated receiver
(Pi loopback :8091) → atomic `completed/` handoff → read-only Observatory
ingestion → SHA-256-addressed archive. No automatic staging, delivery or printing.

The share bridge accepts G-code only until the typed artifact validation and
receiver contracts support other file formats. Never silently rename STEP/STL/3MF
to G-code. A later version should support those types through the same canonical
ingestion service with appropriately revised limits and validation.

## Raspberry Pi commissioning (not automatic)

The existing `lilbitdrop-receiver.service` must be deployed and commissioned first.
Keep both the receiver and this bridge on loopback. Use the existing private
receiver token at `/home/cdaprod/.config/lilbitdrop/receiver-token`.

Run a manual, non-actuating smoke test of the share bridge via the actual Pi,
rather than exposing its loopback port directly to the LAN:

```sh
python3 integrations/lilbitdrop/ios_share.py \
  --token-file /home/cdaprod/.config/lilbitdrop/receiver-token
```

Production deployment requires a **separately reviewed** HTTPS gateway
with a valid certificate and request-size, rate, connection and timeout
limits. Authenticate the upload at the bridge, limit the gateway to trusted
LAN/VPN access, and ensure request URLs and bearer tokens are not logged.
Do not expose port 8092 directly; it has no TLS implementation.

For an iOS Shortcuts proof of concept, configure a Shortcut with:
- **Show in Share Sheet**, accepting Files.
- **Get Details of Files → Name** and an allowlist for supported G-code extensions.
- **Get Contents of URL** using POST with the file as an octet-stream request
  body. Use a reviewed HTTPS URL such as
  `https://printbay.example.invalid/share?filename=<URL-encoded-name>`.
- Set `Authorization: Bearer <private receiver token>` and
  `Content-Type: application/octet-stream`. Store the token in a
  private Shortcut field; avoid screenshots, sharing or iCloud sync of
  credential-bearing Shortcuts. Prefer a scoped share token in a future
  revision rather than reusing the receiver token.
- Display the returned JSON receipt. A receipt indicates receipt, **not**
  completed canonical archive indexing, staging or printing.

The shown URL is illustrative; replace it with the actual private hostname
and TLS setup **after commissioning**. Never use a public anonymous endpoint.

## Acceptance gates

Validate bridge authentication (no token, wrong token, correct token),
truncated upload, invalid filename, incorrect content type, oversized file,
concurrent uploads, loopback-only bind, unresponsive receiver, exact SHA
verification and a real end-to-end handoff. Verify the receiving G-code appears
in Observatory without any staging or printer action. Record the Pi commit,
service states and actual iOS Shortcut test result.

Native AirDrop discovery remains **unverified** until an iPhone actually lists
CDA Neptune PrintBay in its AirDrop picker and a transfer completes. The
fallback Share Sheet upload is a different, explicitly labeled capability.
