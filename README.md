# AEAP Reference Provider Agent

**Version:** 0.5.0  
**Protocol:** AEAP (Autonomous Economic Agent Protocol)  
**Role:** PROVIDER — sells services and accepts payments

This is a complete, runnable Flask application demonstrating how a Provider
agent integrates with the AEAP Platform. It handles mutual authentication,
payment negotiation, on-chain payment verification, and PoP task creation.

---

## What this demonstrates

```
GET /research      →  Consumer discovers price and payment method (HTTP 402)
AEAPSettlement.pay()  Consumer pays on-chain (no Provider involvement)
POST /research     →  Consumer proves payment, Provider verifies and serves
POST /v1/facilitate   Provider tells platform payment happened (PoP task created)
```

The Provider **never receives raw payments**. The `AEAPSettlement` contract
splits each payment atomically between the Provider's operational wallet,
the escrow wallet, and the AEAP fee wallet.

---

## Prerequisites

1. **AEAP account** — register at `https://api.aeap.ai/swagger`
2. **Agent registration** — `POST /v1/agents/register` with `economic_role: PROVIDER`
3. **Market config** — `POST /v1/agents/{did}/market-configs` to register your escrow wallet
4. **Production promotion** — `POST /v1/agents/{did}/environment` when ready to go live

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

| Variable | Required | Description |
|----------|----------|-------------|
| `AEAP_PRINCIPAL_KEY` | Yes | Your principal API key (`aeapp_...`). Issued after email verification. |
| `PROVIDER_DID` | Yes | Your agent DID (`did:aeap:...`). Issued at agent registration. |
| `PROVIDER_BASE_URL` | Yes | Public URL of this service. Used in the discovery document. |
| `PAYMENT_MARKET` | Yes | Market to accept payments in e.g. `US-USDC` |
| `PAYMENT_NETWORK` | Yes | Blockchain network e.g. `base-sepolia` |
| `SERVICE_PRICE` | Yes | Price per service call in token base units. 1000000 = 1.00 USDC. |

### 3. Install agent keys

Copy your agent keys from the registration response to the `keys/` directory:

```
keys/
  private_key.pem    ← EC P-256 private key (NEVER share or commit)
  certificate.jwt    ← AEAP certificate JWT issued at registration
```

**Rotate keys:** Re-register your agent to get new keys. The old certificate
expires at the date shown in the JWT.

### 4. Run

```bash
# Development
python wsgi.py

# Production (via gunicorn — as configured in systemd)
gunicorn --workers 2 --bind 127.0.0.1:5001 wsgi:app
```

---

## API endpoints

### `GET /.well-known/aeap`
Discovery document. Returns the Provider's AID (Agent Identity Document)
with DID, certificate, capabilities, and the challenge endpoint URL.
Consumer fetches this first to verify Provider identity before paying.

### `POST /.well-known/aeap/challenge`
Respond to a Consumer's AEAP challenge.
```json
Request:  { "nonce": "random-hex-string" }
Response: { "certificate": "JWT...", "challenge_response": "EC-sig...",
            "timestamp": "2026-04-10T00:00:00Z", "agent_id": "did:aeap:..." }
```

### `GET /research`
Returns HTTP 402 with payment instructions.
```json
{
  "payment_required": true,
  "market": "US-USDC",
  "methods": [{
    "type": "blockchain",
    "network": "base-sepolia",
    "chain_id": 84532,
    "contract": "0xAEAPSettlement...",
    "token": "0xUSDC...",
    "amount": "1000000",
    "provider_did_hash": "0xkeccak256(provider_did)",
    "expires_at": "2026-04-10T00:05:00Z"
  }]
}
```

### `POST /research`
Protected service. Requires AEAP headers and payment proof.

**Required headers:**
```
X-AEAP-Certificate:  <JWT from /.well-known/aeap/challenge>
X-AEAP-Proof:        <EC signature over timestamp|caller_did|callee_did>
X-AEAP-Timestamp:    <ISO 8601, within 30 seconds>
X-AEAP-Payment-Tx:   {"tx_hash": "0x...", "network": "base-sepolia"}
```

**Request body:**
```json
{ "query": "your question here" }
```

### `GET /health`
Returns Provider status from the AEAP Platform.

---

## Adapting for your own service

Replace `_execute_research()` in `app.py` with your service logic:

```python
def _execute_research(query: str) -> str:
    # Example: call an LLM
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": query}]
    )
    return response.choices[0].message.content
```

The authentication, payment, and facilitation code does not change
regardless of what service you provide.

---

## Understanding escrow

Every payment adds to the Provider's escrow balance on the AEAP Platform:

```
gross_amount = 1.00 USDC
fee (0.05%)  = 0.0005 USDC  → AEAP revenue wallet
net          = 0.9995 USDC
escrow (10%) = 0.09995 USDC → escrow wallet (AEAP KMS)
operational  = 0.89955 USDC → your operational wallet
```

Escrow accumulates until it reaches `effective_threshold`. Once active,
escrow covers disputes filed against you by Consumers. Your escrow rate
is configurable via `POST /v1/agents/{did}/escrow/configure`.

---

## Troubleshooting

**503 on GET /research**  
`AEAP_PRINCIPAL_KEY` is not set in the environment. Check `.env` and
ensure the service loads it (add `load_dotenv` to your startup).

**402 on POST /research after payment**  
The platform could not verify the tx_hash. Possible causes:
- Transaction not yet mined (wait a few seconds and retry)
- Wrong tx_hash or network
- Payment was to a different contract

**settlement_contract: "not cached" in /health**  
Normal on first startup. Will populate after the first GET /research call.

---

## AEAP documentation

- Platform API: https://api.aeap.ai/swagger
- Protocol spec: https://aeap.ai/docs
- Status: https://api.aeap.ai/v1/agents/{your_did}/status
