# Nustro Reference Provider Agent

**Protocol:** AEA/P (Autonomous Economic Agent Protocol)
**Operator:** Nustro — `https://api.nustro.ai`
**Role:** PROVIDER — sells services and accepts payments

A complete, runnable Flask reference implementation of an **AEA/P** provider
agent, integrated with the **Nustro** API. It handles mutual authentication,
payment negotiation, on-chain payment verification, and PoP task creation.

> This is a **Nustro** reference tool. The product/operator surface is Nustro;
> the wire protocol it speaks — `did:aeap:` identifiers, `AEAP-*` handshake
> headers, `.well-known/aeap` discovery — is **AEA/P** and is left as protocol
> surface on purpose.

---

## Architecture (who talks to whom)

The **Platform is out of the runtime path** — onboarding, config, and discovery
only. At runtime the Provider talks **directly** to the Consumer (agent↔agent)
and to the **Nustro Operator** (payment intent, proof verification,
facilitation, status). The Provider **never receives raw payments**: the
`NustroSettlement` contract splits each payment atomically between the
Provider's operational wallet, the escrow wallet, and the Nustro fee.

---

## What this demonstrates

```
GET /research?consumer_did=… → Provider asks the Operator for a payment intent.
                                The Operator enforces the CONSUMER's spend policy
                                and mints the intent; Provider answers HTTP 402
                                (or relays 403 spend_policy_violation on refusal).
NustroSettlement.pay()       → Consumer pays on-chain (no Provider involvement).
POST /research               → Consumer proves payment; Provider verifies + serves.
POST /v1/facilitate          → Provider tells the Operator payment happened; the
                                Operator reads the Settled event, credits escrow,
                                and opens a PoP task.
```

---

## Prerequisites

1. **Nustro account** — register at `https://api.nustro.ai/docs`.
2. **Agent registered + activated** — `POST /v1/agents` (`economic_role: PROVIDER`),
   then `POST /v1/agents/{did}/activate` for the key pair + certificate
   (private key shown **once**).
3. **Markets + wallet** — `POST /v1/agents/{did}/scope/authorized_markets` with a
   per-network `operational_wallet`; the Operator provisions the KMS escrow
   wallet and registers you on the `NustroSettlement` contract.
4. **Production** — `POST /v1/agents/{did}/environment` before a live settlement
   run (needs an accredited Platform + a `nustro_live_` key).

> The settlement gate is the **buyer's** country-derived market vs this
> provider's `authorized_markets` (must include `{BUYER_COUNTRY}-USDC` or
> `GLOBAL-USDC`). `PAYMENT_MARKET` below is cosmetic on the 402.

---

## Run it locally

```bash
git clone https://github.com/aeap-labs/nustro-reference-provider.git
cd nustro-reference-provider
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python wsgi.py                      # serves on http://localhost:5001
```

Then open **http://localhost:5001** — the local console. Paste your agent's
DID, private key (PEM) and certificate (JWT) from activation, set the price,
and hit **Save session**. No `.env` needed: the console posts to
`/configure`, and the keys stay in the process's memory.

Leave this running, then start the
[Consumer](https://github.com/aeap-labs/nustro-reference-consumer) and drive
the interaction from its console.

<details>
<summary><b>Alternative: configure with a .env file</b> (for an unattended / deployed instance)</summary>

```bash
cp .env.example .env      # then edit
```

| Variable | Required | Description |
|----------|----------|-------------|
| `OPERATOR_URL` | No | Nustro Operator base URL. Default `https://api.nustro.ai`. |
| `NUSTRO_PRINCIPAL_KEY` | Yes | Management key (`nustro_sandbox_…` / `nustro_live_…`), shown once. |
| `PROVIDER_DID` | Yes | This agent's DID (`did:aeap:…`). |
| `PROVIDER_BASE_URL` | Yes | Public URL of this service (in the discovery document). |
| `PAYMENT_MARKET` | No | Market label, e.g. `US-USDC`. |
| `PAYMENT_NETWORK` | No | Blockchain network. Default `base-sepolia`. |
| `SERVICE_PRICE` | No | Price per call in **token base units** (10000000 = 10.00 USDC). |
| `BASE_SEPOLIA_RPC` | Yes | RPC URL for the settlement network. |

Plus this agent's material in `keys/`:

```
keys/
  private_key.pem    ← EC P-256 private key (NEVER share or commit)
  certificate.jwt    ← AEA/P certificate JWT (issued by the Nustro CA)
```

With `.env` + `keys/` present the app self-configures at startup and the
console just shows its status.

```bash
gunicorn --workers 2 --bind 127.0.0.1:5001 wsgi:app   # prod
```
</details>

Rotate the key via `POST /v1/agents/{did}/rotate-key` (new key returned once).

---

## Endpoints

- **`GET /`** — **local console**: a browser UI to paste this agent's identity
  (DID, key, certificate), set the service price/market, and check readiness.
  Start the app and open `http://localhost:5001`, then drive the run from the
  Consumer console.
- **`GET /.well-known/aeap`** — discovery document (AID: DID, certificate, capabilities, challenge URL).
- **`POST /.well-known/aeap/challenge`** — respond to a Consumer's AEA/P challenge → `{certificate, challenge_response, timestamp, agent_id}`.
- **`GET /research?consumer_did=…`** — `402` with payment instructions (contract, token, amount, `provider_did_hash`, `expires_at`), or `403 spend_policy_violation` when the Operator refuses the consumer's spend policy.
- **`POST /research`** — protected service. Required headers:
  ```
  AEAP-Certificate:  <JWT from the challenge response>
  AEAP-Proof:        <EC signature over timestamp|caller_did|callee_did>
  AEAP-Timestamp:    <ISO 8601, within 30 s>
  AEAP-Payment-Tx:   {"tx_hash": "0x...", "network": "base-sepolia"}
  ```
- **`POST /configure`** — set the agent identity + service config at runtime
  (for a UI), instead of `.env`. Body: `provider_did`, `provider_base_url`,
  `private_key` (PEM), `certificate` (JWT) — required; `operator_url`,
  `nustro_principal_key`, `payment_market`, `payment_network`, `service_price`
  — optional. **Local/trusted use only** — it accepts a private key over HTTP.
  Until configured (via `.env` or this call), the service routes return
  `409 not_configured`.
- **`GET /health`** — Provider status from the Nustro Operator (`unconfigured` until an identity is loaded).

---

## Payment intent + spend policy (Phase 2)

When the Consumer calls `GET /research?consumer_did=…`, this Provider quotes its
price and asks the Operator for a payment intent, passing `amount` (whole
currency units, derived from `SERVICE_PRICE`) and its own `provider_did`. The
**Operator** — not the Provider — enforces the **consumer's** spend policy
(`max_transaction_value`, `spending_limit`, counterparty floors) at intent
creation. On refusal the Operator returns `403 spend_policy_violation`, which
this Provider relays to the Consumer (no 402 is issued). No intent, no
settlement.

---

## Adapting for your own service

Replace `_execute_research()` in `app.py` with your service logic. The
authentication, payment, and facilitation code does not change.

---

## Understanding escrow

Every settlement credits the Provider's escrow on Nustro (read the split off
the `Settled` event):

```
gross        = 1.00 USDC
fee          → Nustro fee
escrow (10%) → escrow wallet (Nustro KMS)
operational  → your operational wallet
```

Escrow accrues to `effective_threshold`; once active it backs disputes filed
against you.

---

## Troubleshooting

- **`503` on `GET /research`** — `NUSTRO_PRINCIPAL_KEY` unset, or the Operator is unreachable.
- **`403 spend_policy_violation` on `GET /research`** — the Operator refused the intent under the **consumer's** spend policy (`detail.failed_check`). Not a Provider error — the consumer's principal must widen its scope.
- **`402` on `POST /research` after payment** — the Operator couldn't verify the tx (not yet mined / wrong tx or network / wrong contract).
- **`settlement_contract: not cached` in `/health`** — normal before the first `GET /research`.

---

## Documentation

- Nustro API (contract + Swagger): https://api.nustro.ai/docs
- AEA/P protocol spec: https://docs.aeap.dev
