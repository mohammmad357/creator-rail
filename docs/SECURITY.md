# Security boundaries

This is a local experimental prototype, not an audited production payment platform.

- No private keys, recovery phrases, wallet creation, signing, withdrawals or asset conversion are implemented.
- Demo addresses are generated public witness programs without spendable keys. All synthetic states remain labelled, including in CSV.
- BTCPay credentials stay server-side. API errors do not return upstream response bodies or secrets. Sensitive payment-method data is not requested.
- Only the configured store, BTC amount and on-chain method are accepted. Address checksum/network prefix and checkout origin are checked. Actual testnet/signet identity must still be verified independently.
- Status is re-read from the configured processor; provider downtime returns an error instead of a new confirmation. Re-reading can move a settled snapshot back to processing. Snapshot storage is not a duplicate monetary ledger.
- A public invoice locator is a high-entropy capability to view its status/destination. It is not authentication of the donor. No rewards or public donor claims depend on it.
- All app scripts/styles are served locally; a restrictive CSP, no-referrer and nosniff headers are set. Only JSON and same-origin browser POSTs are accepted; invoice creation is rate-limited. The middleware Host check is defence in depth, not peer authentication: keep Uvicorn bound to loopback.
- The small local service buffers POST bodies; the 16KB validation is not an upstream streaming-body or network DoS guarantee. A public deployment requires reviewed request limits at the server/proxy, authentication, trusted-host configuration, HTTPS, persistence and operational monitoring.
- BTCPay mode CSV requires the configured administrator bearer token. The synthetic CSV is intentionally public within the local demo and contains no real personal data.
- Public creator registration, public messages and multi-tenant authorization are outside this version. There is only one configured creator/store. Claiming wallet control alone would not establish legal eligibility.

Use a privately controlled experimental instance. Never paste secrets into issues, chat, screenshots or the repository. Dependency/provider terms and legal requirements remain separate from the source licence.

Potential vulnerabilities should be reproduced without real funds or secrets. Do not describe this release as audited, production-ready or universally available.
