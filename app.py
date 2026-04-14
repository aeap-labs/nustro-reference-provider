"""
AEAP Reference Provider Agent
==============================
Version: 0.5.0
Protocol: AEAP (Autonomous Economic Agent Protocol)

This file demonstrates how a Provider agent integrates with the AEAP Platform.
It is a complete, runnable Flask application — not a tutorial or pseudocode.

WHAT THIS DEMONSTRATES
-----------------------
The AEAP payment flow from the Provider side:

  Phase 1 — AEAP mutual authentication
    Consumer fetches the Provider's discovery document and verifies its
    identity using the AEAP CA certificate before sending any money.

  Phase 2 — Payment negotiation (402 Payment Required)
    Consumer calls GET /research. Provider returns HTTP 402 with complete
    AEAPSettlement.pay() instructions. No wallet addresses are exchanged —
    the consumer pays the AEAPSettlement contract directly.

  Phase 4 — Service call with payment proof
    Consumer calls POST /research with:
      - AEAP bound proof headers (proves Consumer identity)
      - X-AEAP-Payment-Tx header (proves payment was submitted on-chain)

  Phase 5 — Facilitation (platform verifies payment)
    Provider calls POST /v1/facilitate. The AEAP Platform reads the
    on-chain Settled event, verifies both DID hashes, updates escrow
    balances, and creates a PoP task record.

  Phase 6 — Service execution
    After payment is verified, the Provider executes the service and
    returns the result. In production, replace _execute_research() with
    your actual service logic.

REPLACING /research WITH YOUR OWN SERVICE
------------------------------------------
This app uses a simple echo service at /research. To use your own:

  1. Replace _execute_research(query) with your service logic.
  2. Change the endpoint path (GET/POST /research) to match your API.
  3. Update SERVICE_PRICE to your pricing.
  4. Update PAYMENT_MARKET and PAYMENT_NETWORK if targeting a different chain.
  5. The authentication, payment, and facilitation code does not change.

REQUIRED ENVIRONMENT VARIABLES
--------------------------------
See .env.example for all required variables with descriptions.

ENDPOINTS
----------
  GET  /.well-known/aeap           — discovery document (public)
  POST /.well-known/aeap/challenge — respond to AEAP challenge (public)
  GET  /research                   — returns 402 with payment instructions
  POST /research                   — protected service (requires payment proof)
  GET  /health                     — health check (public)

AEAP DOCUMENTATION
-------------------
  Platform API:     https://api.aeap.ai/swagger
  Protocol spec:    https://aeap.ai/docs
  Sandbox:          https://provider.sandbox.aeap.ai
"""

import json
import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file.
# This must happen before importing anything that reads os.environ.
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

import requests as http_requests
from flask import Flask, request, jsonify
from aeap_client import AEAPClient

app = Flask(__name__)

# ── Agent configuration ────────────────────────────────────────────────────────
# PROVIDER_DID: your agent's DID, issued by the AEAP Platform at registration.
# Replace with your own DID — do NOT use this DID in production.
PROVIDER_DID = os.environ.get('PROVIDER_DID', 'did:aeap:4ad3c2d3-a658-4793-8c5a-75eae395a053')

# BASE_URL: the public URL of this Provider service.
# Used in the discovery document so Consumers know where to challenge you.
BASE_URL = os.environ.get('PROVIDER_BASE_URL', 'https://provider.sandbox.aeap.ai')

# PLATFORM_URL: the AEAP Platform API base URL.
# Sandbox and production share the same platform.
PLATFORM_URL = 'https://api.aeap.ai'

# ── Payment configuration ──────────────────────────────────────────────────────
# PAYMENT_MARKET: the market this Provider accepts payments in.
# Format: {jurisdiction}-{currency} e.g. US-USDC, EU-USDC
PAYMENT_MARKET = os.environ.get('PAYMENT_MARKET', 'US-USDC')

# PAYMENT_NETWORK: the blockchain network to accept payments on.
# Must match one of the networks registered in the AEAP Platform.
PAYMENT_NETWORK = os.environ.get('PAYMENT_NETWORK', 'base-sepolia')

# SERVICE_PRICE: price of one service call in token base units.
# USDC has 6 decimals, so 1_000_000 = 1.00 USDC.
# Update this to your actual pricing.
SERVICE_PRICE = int(os.environ.get('SERVICE_PRICE', '1000000'))  # 1.00 USDC

# ── AEAPClient setup ───────────────────────────────────────────────────────────
# AEAPClient handles:
#   - Signing challenge responses (Phase 1)
#   - Generating bound proof headers (for calls TO the platform)
#   - Verifying incoming Consumer certificates and bound proofs (Phase 4)
#   - Fetching the Provider's own status from the platform
#
# Keys are generated at agent registration time.
# Store private_key.pem securely — it cannot be recovered if lost.
client = AEAPClient(
    agent_did        = PROVIDER_DID,
    private_key_path = os.path.join(os.path.dirname(__file__), 'keys', 'private_key.pem'),
    certificate_path = os.path.join(os.path.dirname(__file__), 'keys', 'certificate.jwt'),
    platform_url     = PLATFORM_URL,
)

# ── Payment address cache ──────────────────────────────────────────────────────
# The AEAPSettlement contract address and token address are stable between
# upgrades — we cache them to avoid a platform call on every 402 response.
# The cache is invalidated on process restart, which is acceptable.
_payment_address_cache = {}


def _get_payment_address(consumer_did: str = None) -> dict | None:
    """
    Fetch the AEAPSettlement contract address and ERC-20 token address
    from the AEAP Platform for this market/network combination.

    The contract/token details are cached in memory after the first fetch.
    When consumer_did is provided, a fresh request is made to create a
    payment_intent record on the platform. Payment intents track whether
    the Consumer completes the payment — abandoned intents reduce the
    Consumer's payment_timeliness PoP signal.

    Returns a dict with: contract, token, chain_id, currency, decimals
    and optionally: intent_id, expires_at (when consumer_did is provided).
    Returns None if the platform is unreachable or the config is missing.
    """
    market  = os.environ.get('PAYMENT_MARKET', PAYMENT_MARKET)
    network = os.environ.get('PAYMENT_NETWORK', PAYMENT_NETWORK)
    key     = os.environ.get('AEAP_PRINCIPAL_KEY', '')

    cache_key = f"{market}:{network}"

    # Populate the cache if empty (first request after startup)
    if cache_key not in _payment_address_cache:
        try:
            resp = http_requests.get(
                f"{PLATFORM_URL}/v1/payment-address",
                params  = {'market': market, 'network': network},
                headers = {'X-AEAP-Principal-Key': key},
                timeout = 10,
            )
            print(f"[PAYMENT] payment-address fetch: status={resp.status_code}", flush=True)
            if resp.status_code == 200:
                _payment_address_cache[cache_key] = resp.json()
            else:
                print(f"[PAYMENT] fetch failed: {resp.text[:100]}", flush=True)
        except Exception as e:
            print(f"[PAYMENT] fetch exception: {e}", flush=True)

    base = _payment_address_cache.get(cache_key)
    if not base:
        return None  # Platform unreachable or misconfigured

    # If consumer_did is provided, make a fresh request to auto-create a
    # payment_intent on the platform. The intent expires in 5 minutes.
    # If not paid within that window, it's marked ABANDONED by the platform.
    if consumer_did:
        try:
            resp = http_requests.get(
                f"{PLATFORM_URL}/v1/payment-address",
                params  = {'market': market, 'network': network, 'consumer_did': consumer_did},
                headers = {'X-AEAP-Principal-Key': key},
                timeout = 10,
            )
            if resp.status_code == 200:
                return resp.json()  # Includes intent_id + expires_at
        except Exception as e:
            print(f"[PAYMENT] intent creation failed: {e}", flush=True)
            # Fall through to cached data — intent creation is optional

    return base


def _get_provider_did_hash() -> str:
    """
    Compute keccak256(provider_did) — required by AEAPSettlement.pay().

    The Consumer includes this in their on-chain pay() call so the
    AEAPSettlement contract can look up the Provider's registered wallets
    (operational, escrow) and split the payment correctly.
    """
    from web3 import Web3
    return Web3.keccak(text=PROVIDER_DID).hex()


# ── Discovery endpoints ────────────────────────────────────────────────────────
# These are the AEAP standard well-known endpoints. Every AEAP Provider
# MUST implement them. Consumers call these before sending any request.

@app.route('/.well-known/aeap', methods=['GET'])
def discovery():
    """
    Phase 1 — Discovery document.

    Returns the Provider's AID (Agent Identity Document) containing:
      - The Provider's DID and certificate
      - Where to send AEAP challenges (Phase 1)
      - The Provider's economic role, capabilities, and escrow state

    Consumers fetch this to verify they're talking to a legitimate
    AEAP-certified agent before initiating payment.
    """
    return jsonify(
        client.discovery_document(
            challenge_endpoint=f"{BASE_URL}/.well-known/aeap/challenge"
        )
    )


@app.route('/.well-known/aeap/challenge', methods=['POST'])
def challenge():
    """
    Phase 1 — Respond to Consumer's challenge nonce.

    The Consumer sends a random nonce. The Provider signs it with its
    EC private key and returns the signature alongside its certificate JWT.
    The Consumer verifies the signature offline (no platform call needed).

    This proves the Provider controls the private key matching the
    public key in its AEAP certificate — i.e., it is who it claims to be.

    Request:  { "nonce": "..." }
    Response: { "certificate": "JWT...", "challenge_response": "EC sig...",
                "timestamp": "ISO 8601", "agent_id": "did:aeap:..." }
    """
    data = request.get_json()
    if not data or not data.get('nonce'):
        return jsonify({'error': 'missing_fields', 'message': 'nonce required'}), 400

    nonce   = data['nonce']
    headers = client.get_challenge_response_headers(nonce)

    return jsonify({
        'certificate':        headers['X-AEAP-Certificate'],
        'challenge_response': headers['X-AEAP-Challenge-Response'],
        'timestamp':          headers['X-AEAP-Timestamp'],
        'agent_id':           PROVIDER_DID,
    })


# ── Service endpoint ───────────────────────────────────────────────────────────

@app.route('/research', methods=['GET'])
def research_payment_required():
    """
    Phase 2 — Return 402 Payment Required with payment instructions.

    The Consumer calls GET /research first to discover:
      - How much to pay (amount in token base units)
      - Which contract to call (AEAPSettlement address)
      - Which token to use (USDC contract address)
      - The Provider's DID hash (required by AEAPSettlement.pay())
      - When the quote expires (Consumer must pay before this time)

    The Consumer then:
      1. Approves the ERC-20 token spend (token.approve)
      2. Calls AEAPSettlement.pay() on-chain
      3. Calls POST /research with the tx_hash as proof

    IMPORTANT: We return the Provider DID hash, NOT a wallet address.
    The Consumer pays the AEAPSettlement contract — it handles routing
    funds to the Provider's operational wallet, escrow wallet, and
    the AEAP fee wallet atomically. The Provider never receives raw payments.

    If consumer_did is passed as a query param, a payment intent is created
    on the platform so Consumer abandonment can be tracked for PoP rating.
    """
    # Consumer may pass their DID so we can create a payment intent
    consumer_did = request.args.get('consumer_did', '') or None

    payment_addr = _get_payment_address(consumer_did=consumer_did)
    if not payment_addr:
        # Platform unreachable — return 503 so Consumer can retry
        return jsonify({
            'error':   'payment_config_unavailable',
            'message': 'Provider payment configuration is temporarily unavailable.',
        }), 503

    import datetime
    # Quote expires in 5 minutes — Consumer must pay before this time.
    # After expiry, the payment intent (if created) is marked ABANDONED.
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc) +
        datetime.timedelta(minutes=5)
    ).isoformat()

    print(f"[PAYMENT] Returning 402 — amount={SERVICE_PRICE} network={PAYMENT_NETWORK}", flush=True)

    return jsonify({
        'payment_required': True,
        'market':           PAYMENT_MARKET,
        'methods': [
            {
                'type':              'blockchain',
                'network':           PAYMENT_NETWORK,
                'chain_id':          payment_addr.get('chain_id'),
                'contract':          payment_addr.get('contract'),
                'token':             payment_addr.get('token'),
                'decimals':          payment_addr.get('decimals', 6),
                'amount':            str(SERVICE_PRICE),
                'provider_did_hash': _get_provider_did_hash(),
                'expires_at':        expires_at,
            }
        ],
        'provider_did': PROVIDER_DID,
    }), 402


@app.route('/research', methods=['POST'])
def research():
    """
    Phases 4-6 — Verify Consumer identity, verify payment, execute service.

    This endpoint does three things in sequence:

    PHASE 4a — Verify Consumer identity (AEAP bound proof)
      The Consumer includes X-AEAP-Certificate and X-AEAP-Proof headers.
      We verify the certificate was signed by the AEAP CA and the proof
      signature binds the Consumer's DID to this Provider's DID.
      This is done OFFLINE — no platform call needed for verification.

    PHASE 4b — Verify payment proof header
      The Consumer includes X-AEAP-Payment-Tx: { tx_hash, network }.
      This is NOT on-chain verification — that happens in Phase 5.

    PHASE 5 — Facilitation (platform verifies payment on-chain)
      We call POST /v1/facilitate with the tx_hash. The AEAP Platform:
        - Reads the Settled event from the blockchain
        - Verifies the Provider DID hash matches our agent
        - Verifies the Consumer DID hash matches the verified Consumer
        - Updates our escrow balance
        - Creates a PoP task record (if different principals)
        - Returns facilitation_id and task_id

    PHASE 6 — Service execution
      Only after payment is verified do we execute the service.
      _execute_research() is the placeholder — replace with real logic.

    Required headers:
      X-AEAP-Certificate    — Consumer's AEAP certificate JWT
      X-AEAP-Proof          — EC signature: timestamp|caller_did|callee_did
      X-AEAP-Timestamp      — ISO 8601 timestamp (must be within 30 seconds)
      X-AEAP-Payment-Tx     — JSON: { "tx_hash": "0x...", "network": "..." }

    Request body: { "query": "..." }
    """
    # Phase 4a: Verify Consumer AEAP identity
    # verify_incoming() checks the certificate JWT signature against the
    # AEAP CA public key and verifies the bound proof EC signature.
    # Both are done offline — no platform round-trip needed.
    verification = client.verify_incoming(
        request_headers = request.headers,
        own_did         = PROVIDER_DID,
    )

    if not verification['verified']:
        reason = verification.get('reason', 'verification_failed')
        if reason == 'missing_headers':
            # Consumer didn't include AEAP headers — they need to pay first
            return jsonify({
                'error':   'certificate_required',
                'message': 'AEAP certification required. GET /research for payment instructions.',
            }), 401
        return jsonify({'error': 'aeap_verification_failed', 'reason': reason}), 401

    consumer_did = verification['caller_did']
    print(f"[AUTH] Consumer verified: {consumer_did}", flush=True)

    # Phase 4b: Extract payment proof from header
    # The Consumer provides the tx_hash from their AEAPSettlement.pay() call.
    # We don't verify it here — the platform does that in Phase 5.
    payment_tx_header = request.headers.get('X-AEAP-Payment-Tx')
    tx_hash    = None
    tx_network = None

    if payment_tx_header:
        try:
            payment_tx = json.loads(payment_tx_header)
            tx_hash    = payment_tx.get('tx_hash')
            tx_network = payment_tx.get('network')
        except Exception:
            return jsonify({
                'error':   'invalid_payment_header',
                'message': 'X-AEAP-Payment-Tx must be valid JSON: { "tx_hash": "0x...", "network": "..." }',
            }), 400

    if not tx_hash:
        return jsonify({
            'error':   'payment_required',
            'message': 'Payment required. GET /research for payment instructions, then include X-AEAP-Payment-Tx.',
        }), 402

    # Phase 5: Facilitation
    # Tell the AEAP Platform about the payment. It reads the blockchain
    # event, verifies correctness, and creates the PoP task record.
    # This is the only platform call in the service flow.
    print(f"[FACILITATE] tx={tx_hash} consumer={consumer_did}", flush=True)
    facilitation = _facilitate(
        consumer_did = consumer_did,
        tx_hash      = tx_hash,
        network      = tx_network or PAYMENT_NETWORK,
    )

    if not facilitation.get('success'):
        # Payment verification failed — could be wrong tx, wrong amount,
        # wrong DID hash, or tx not yet on-chain. Return 402.
        return jsonify({
            'error':   'payment_verification_failed',
            'message': facilitation.get('message', 'Payment could not be verified.'),
            'detail':  facilitation.get('detail'),
        }), 402

    # Phase 6: Execute service
    # Only reached after payment is cryptographically verified on-chain.
    # Replace _execute_research() with your actual service logic.
    data = request.get_json()
    if not data or not data.get('query'):
        return jsonify({'error': 'missing_fields', 'message': 'query required'}), 400

    query  = data['query']
    result = _execute_research(query)

    import uuid
    interaction_id = str(uuid.uuid4())

    print(f"[RESEARCH] interaction={interaction_id} consumer={consumer_did}", flush=True)

    # Return facilitation_id and task_id so Consumer can confirm the PoP task
    return jsonify({
        'interaction_id':       interaction_id,
        'result':               result,
        'provider':             PROVIDER_DID,
        'consumer':             consumer_did,
        'consumer_environment': verification.get('environment', 'unknown'),
        'consumer_pop_rating':  verification.get('pop_rating'),
        'verified':             True,
        'facilitation_id':      facilitation.get('facilitation_id'),
        'task_id':              facilitation.get('task_id'),
    })


def _facilitate(consumer_did: str, tx_hash: str, network: str) -> dict:
    """
    POST /v1/facilitate — tell the AEAP Platform a payment occurred.

    The platform reads the AEAPSettlement Settled event from the blockchain,
    verifies all amounts and DID hashes, updates the Provider's escrow
    balance, and creates a PoP task record for the interaction.

    This is the ONLY call where we send the tx_hash to the platform.
    The platform never submits any transactions — it is always read-only.

    Returns a dict with success flag and facilitation details including
    task_id (None if same-principal — Sybil check prevents PoP gaming).
    """
    principal_key = os.environ.get('AEAP_PRINCIPAL_KEY', '')
    try:
        resp = http_requests.post(
            f"{PLATFORM_URL}/v1/facilitate",
            headers = {
                'X-AEAP-Principal-Key': principal_key,
                'Content-Type':         'application/json',
            },
            json = {
                'provider_did': PROVIDER_DID,
                'consumer_did': consumer_did,
                'tx_hash':      tx_hash,
                'network':      network,
            },
            timeout=30,  # blockchain reads can take a few seconds
        )
        data = resp.json()
        if resp.status_code == 200:
            print(
                f"[FACILITATE] OK — escrow_credited={data.get('escrow_credited')} "
                f"escrow_state={data.get('escrow_state')} task={data.get('task_id')}",
                flush=True
            )
            return {'success': True, **data}
        else:
            print(f"[FACILITATE] Failed: {resp.status_code} {data}", flush=True)
            return {
                'success': False,
                'message': data.get('message', 'Facilitation rejected'),
                'detail':  data.get('reason'),
            }
    except Exception as e:
        print(f"[FACILITATE] Exception: {e}", flush=True)
        return {'success': False, 'message': str(e)}


def _execute_research(query: str) -> str:
    """
    Execute the Provider's service.

    THIS IS THE PLACEHOLDER — replace with your actual service logic.

    Examples:
      - Call an LLM API (OpenAI, Anthropic, etc.)
      - Query a database or knowledge base
      - Execute a computation or data transformation
      - Call an external API

    The function receives the Consumer's query and returns a string result.
    You can return any JSON-serializable data by modifying the research()
    endpoint's response format.
    """
    return (
        f"Research result for: '{query}'. "
        f"This is the AEAP reference Provider — payment verified via AEAPSettlement. "
        f"In production this would call an LLM, database, or external API."
    )


# ── Health check ───────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """
    Health check endpoint.

    Returns the Provider's current status from the AEAP Platform,
    including escrow state and PoP rating. Useful for monitoring
    and debugging.

    settlement_contract will show 'not cached' until the first
    GET /research call populates the payment address cache.
    """
    status       = client.get_own_status()
    payment_addr = _payment_address_cache.get(f"{PAYMENT_MARKET}:{PAYMENT_NETWORK}")
    return jsonify({
        'status':              'ok',
        'agent_id':            PROVIDER_DID,
        'role':                'PROVIDER',
        'agent_status':        status.get('status')       if status else 'unknown',
        'environment':         status.get('environment')  if status else 'unknown',
        'cert_tier':           status.get('cert_tier')    if status else 'unknown',
        'escrow_state':        status.get('escrow_state') if status else 'unknown',
        'payment_market':      PAYMENT_MARKET,
        'payment_network':     PAYMENT_NETWORK,
        'service_price_usdc':  SERVICE_PRICE / 1_000_000,
        'settlement_contract': payment_addr.get('contract') if payment_addr else 'not cached',
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
