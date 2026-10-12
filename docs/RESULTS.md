# Validation record — 2026-10-12

Status: **local experimental prototype**, default synthetic regtest fixtures. No real funds, wallet keys, financial account or blockchain node was used.

## Executed checks

- `python -m pytest -q`: **47 passed**, 1 deprecation warning, 3.60 seconds in the recorded Windows/Python 3.13.7 environment.
- `node --check static/app.js`: passed.
- A second, fresh virtual environment installed the pinned requirements; imports, homepage response and creation of a synthetic 1,000-satoshi invoice passed.
- Browser: Persian ↔ English switches language and direction; a 390px viewport showed no horizontal overflow in either language. This is a viewport check, not a physical touchscreen test or an accessibility audit.
- Keyboard: Tab moved from amount to the first preset; Enter selected 1,000 and submitted a synthetic invoice. An amount of 99 was rejected by browser validation.
- Browser synthetic cases observed: partial amount 5,000 of 10,000; settled back to processing; a manually marked case displays "Needs review" with observed amount zero.
- The **actual browser-rendered QR** for a newly created 1,000-satoshi fixture was independently decoded with ZXing. Destination, `bcrt1` regtest prefix and `amount=0.00001000` matched the displayed invoice. This validates rendering/encoding only, not wallet ownership or funds movement.
- Backup/restore test copied a verified SQLite snapshot into a fresh database and recovered both CSV and the synthetic partial-payment state after a new app instance.

Tests also reject invalid precision, float/boolean amounts, provider store/currency/amount mismatches, wrong-network or invalid checksums, an untrusted checkout origin, duplicate payment methods, forged webhooks, cross-origin POSTs, untracked invoices and unauthorized real-adapter report requests. Provider downtime does not confirm payment and does not consume a webhook delivery, allowing a retry.

The webhook test intentionally supplies a **validly signed but false settlement claim**: the mocked authoritative invoice remains New, so the app does not confirm it. A later duplicate refresh observes the mocked Settled state without adding a second invoice/delivery. These are mocked HTTP contract tests, not transactions on a network.

## Warning

The installed Starlette test client emitted a deprecation warning for its `httpx` compatibility path. Tests passed. The lock captures the tested versions; this warning is retained rather than hidden. Future dependency changes require relevant revalidation.

## Not completed

Docker was not found on this device. No real BTCPay/Bitcoin Core regtest deployment, testnet wallet transaction, independent three-person usability review, five-creator interview, hosted pilot, second network or customer revenue has been verified. A live payment service and receipt/settlement eligibility have not been established.

The API contract source was inspected and its digest retained in `api-contract.json`. Installation is not a security audit and software availability does not establish legal/provider eligibility.
