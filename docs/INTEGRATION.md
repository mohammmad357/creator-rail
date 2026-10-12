# Experimental BTCPay integration

The adapter was written against the official [Greenfield schema](https://docs.btcpayserver.org/API/Greenfield/v1/swagger.json), inspected on 2026-10-12. Its normalized contract and source digest are in `api-contract.json`. This is documentation inspection and mocked HTTP validation; a live processor connection is still required.

## Prerequisite

Provide your own experimental BTCPay/Bitcoin Core installation. Follow the [official deployment guide](https://docs.btcpayserver.org/FAQ/Deployment/). Docker was not available in the development environment; no BTCPay instance or Bitcoin node was installed in this task. Do not deploy the payment business on GitHub Pages.

Start with regtest and a store whose BTC on-chain destination is a checksummed `bcrt1` witness-v0 P2WPKH address. This version intentionally rejects legacy and Taproot address types. For testnet/signet, only `tb1` witness-v0 P2WPKH is accepted. Prefix checks cannot distinguish testnet from signet; verify the processor's actual network independently before use. No mainnet mode is supported.

## Configuration

Set these environment variables locally; example placeholders are not working credentials:

```text
CREATOR_MODE=btcpay
CREATOR_NETWORK=regtest
BTCPAY_URL=http://127.0.0.1:23000
BTCPAY_STORE_ID=YOUR_EXPERIMENTAL_STORE
BTCPAY_API_KEY=LOCAL_SECRET_NOT_COMMITTED
BTCPAY_WEBHOOK_SECRET=AT_LEAST_32_RANDOM_CHARACTERS
CREATOR_ADMIN_KEY=AT_LEAST_32_RANDOM_CHARACTERS
```

BTCPay URL must be an origin. Only local loopback origins can use HTTP; a remote origin requires HTTPS. No redirected requests or proxy environment credentials are used by the adapter. Configure an API key scoped to this store and only `btcpay.store.cancreateinvoice` and `btcpay.store.canviewinvoices`, as described in the inspected schema. Webhooks are configured separately by the store owner; the application does not request store-admin or wallet-spending access.

In the BTCPay store, send invoice events to `http://127.0.0.1:8031/api/webhook` only when both services can reach that loopback endpoint on the same host. In Docker, container loopback is not the host loopback; configure a deliberate local networking arrangement. Do not assume these URLs work across containers or a remote host. Public tunnelling, a reverse proxy and production operations are not configured by this prototype.

The webhook signature is `sha256=HMAC256(UTF8(secret), raw_body)` in `BTCPay-Sig`. Invalid signatures are rejected. The handler re-reads the invoice and payment method from the configured provider, rejects mismatching stores and amounts, and records the original delivery identity once. When the provider is unavailable, the delivery is not marked completed and can be retried. The notification's amount or claimed state is never a payment receipt.

## Mandatory real integration check

Create a regtest invoice through Creator Rail; independently verify store, requested BTC amount, zero payment tolerance, currency and destination in BTCPay. Obtain test funds using your locally controlled Bitcoin regtest setup. Observe New → Processing → Settled through the real processor. Re-send a valid webhook and verify that no extra invoice appears in CSV. Exercise expiry, partial payment, provider downtime and manual marking. Retain logs without credentials.

These steps are instructions, not a report of completed integration. Mainnet receipts, an exchange, stablecoin conversion, custodial wallets and generic multi-tenant operation are not implemented.
