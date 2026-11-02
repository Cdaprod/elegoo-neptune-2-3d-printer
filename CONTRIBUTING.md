# Contributing

Contributions are welcome. Before opening a pull request, describe the user problem and the behavior change, then run the tests and lint checks in [developer documentation](docs/development.md).

For bugs, include steps to reproduce, the relevant app version/commit, sanitized error messages, and hardware/firmware details when available. Remove dashboard tokens, Wi-Fi credentials, private IP addresses, printer logs, and SQLite/runtime files before sharing anything.

Changes that send printer commands must use the existing fixed-command validation, authentication, serialization, timeout, and runtime safety paths. Automated tests must use mocks; do not operate a physical printer as part of CI. State clearly whether evidence is source-based, mocked, firmware-reported, estimated, or operator-observed.

This is a community project without guaranteed support response times. Be respectful and keep discussions focused on reproducible technical details.
