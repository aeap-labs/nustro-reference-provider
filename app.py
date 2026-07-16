"""
Nustro Reference Provider Agent
==============================
Version: 0.5.0
Protocol: AEA/P (Autonomous Economic Agent Protocol)

This file demonstrates how a Provider agent integrates with the Nustro Operator.
It is a complete, runnable Flask application — not a tutorial or pseudocode.

WHAT THIS DEMONSTRATES
-----------------------
The AEA/P payment flow from the Provider side:

  Phase 1 — AEA/P mutual authentication
    Consumer fetches the Provider's discovery document and verifies its
    identity using the Nustro CA certificate before sending any money.

  Phase 2 — Payment negotiation (402 Payment Required)
    Consumer calls GET /research. Provider returns HTTP 402 with complete
    NustroSettlement.pay() instructions. No wallet addresses are exchanged —
    the consumer pays the NustroSettlement contract directly.

  Phase 4 — Service call with payment proof
    Consumer calls POST /research with:
      - AEA/P bound proof headers (proves Consumer identity)
      - AEAP-Payment-Tx header (proves payment was submitted on-chain)

  Phase 5 — Facilitation (Operator verifies payment)
    Provider calls POST /v1/facilitate. The Nustro Operator reads the
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
  POST /.well-known/aeap/challenge — respond to AEA/P challenge (public)
  GET  /research                   — returns 402 with payment instructions
  POST /research                   — protected service (requires payment proof)
  GET  /health                     — health check (public)

NUSTRO DOCUMENTATION
-------------------
  Nustro API:     https://api.nustro.ai/docs
  Protocol spec:    https://docs.aeap.dev
  Sandbox:          http://localhost:5001
"""

import json
import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file.
# This must happen before importing anything that reads os.environ.
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

import requests as http_requests
from flask import Flask, request, jsonify, send_file
from aeap_client import AEAPClient

app = Flask(__name__)

# ── Agent configuration ────────────────────────────────────────────────────────
# PROVIDER_DID: your agent's DID, issued by the Nustro Operator at registration.
# Replace with your own DID — do NOT use this DID in production.
PROVIDER_DID = os.environ.get('PROVIDER_DID', 'did:aeap:4ad3c2d3-a658-4793-8c5a-75eae395a053')

# BASE_URL: the public URL of this Provider service.
# Used in the discovery document so Consumers know where to challenge you.
BASE_URL = os.environ.get('PROVIDER_BASE_URL', 'http://localhost:5001')

# OPERATOR_URL: the Nustro Operator API base URL.
# Sandbox and production share the same Operator.
OPERATOR_URL = os.environ.get('OPERATOR_URL', 'https://api.nustro.ai')

# ── Payment configuration ──────────────────────────────────────────────────────
# PAYMENT_MARKET: the market this Provider accepts payments in.
# Format: {jurisdiction}-{currency} e.g. US-USDC, EU-USDC
PAYMENT_MARKET = os.environ.get('PAYMENT_MARKET', 'US-USDC')

# PAYMENT_NETWORK: the blockchain network to accept payments on.
# Must match one of the networks registered in the Nustro Operator.
PAYMENT_NETWORK = os.environ.get('PAYMENT_NETWORK', 'base-sepolia')

# SERVICE_PRICE: price of one service call in token base units.
# USDC has 6 decimals, so 1_000_000 = 1.00 USDC.
# Update this to your actual pricing.
SERVICE_PRICE = int(os.environ.get('SERVICE_PRICE', '1000000'))  # 1.00 USDC

# ── AEAPClient setup ───────────────────────────────────────────────────────────
# AEAPClient handles:
#   - Signing challenge responses (Phase 1)
#   - Generating bound proof headers (for calls TO the Operator)
#   - Verifying incoming Consumer certificates and bound proofs (Phase 4)
#   - Fetching the Provider's own status from the Operator
#
# Keys are generated at agent registration time.
# Store private_key.pem securely — it cannot be recovered if lost.
# Build the client from keys/ at startup IF a DID + key files are present;
# otherwise stay unconfigured until POST /configure supplies an identity — so
# the app boots for a UI-driven demo with no .env.
def _build_client(agent_did, key_path, cert_path, operator_url):
    try:
        if agent_did and os.path.exists(key_path) and os.path.exists(cert_path):
            return AEAPClient(agent_did=agent_did, private_key_path=key_path,
                              certificate_path=cert_path, operator_url=operator_url)
    except Exception as e:
        print(f"[CONFIG] startup identity not loaded: {e}", flush=True)
    return None

client = _build_client(
    PROVIDER_DID,
    os.path.join(os.path.dirname(__file__), 'keys', 'private_key.pem'),
    os.path.join(os.path.dirname(__file__), 'keys', 'certificate.jwt'),
    OPERATOR_URL,
)


@app.before_request
def _gate_unconfigured():
    """Block the service routes until an identity is loaded. The console (/),
    /configure and /health stay open so the UI can configure and poll readiness."""
    if client is None and request.endpoint not in ('ui', 'configure', 'health', 'static'):
        return jsonify({'error': 'not_configured',
                        'message': 'No agent identity loaded. POST /configure first.'}), 409


@app.route('/', methods=['GET'])
def ui():
    """Local console — configure this agent from the browser (see ui.html)."""
    return send_file(os.path.join(os.path.dirname(__file__), 'ui.html'))


@app.route('/configure', methods=['POST'])
def configure():
    """Set this agent's identity + service config at runtime (for the demo UI),
    instead of reading .env at startup.

    Body: provider_did, provider_base_url, private_key (PEM), certificate (JWT)
    — required; operator_url, nustro_principal_key, payment_market,
    payment_network, service_price — optional.

    LOCAL / TRUSTED DEMO USE ONLY: this accepts an agent private key over HTTP.
    Never expose it on an untrusted network.
    """
    global PROVIDER_DID, BASE_URL, OPERATOR_URL, PAYMENT_MARKET, PAYMENT_NETWORK, SERVICE_PRICE, client
    data = request.get_json(silent=True) or {}

    required = ['provider_did', 'provider_base_url', 'private_key', 'certificate']
    missing  = [k for k in required if not (data.get(k) or '').strip()]
    if missing:
        return jsonify({'error': 'missing_fields', 'missing': missing}), 400

    operator_url = (data.get('operator_url') or OPERATOR_URL).strip()
    try:
        new_client = AEAPClient(
            agent_did=data['provider_did'].strip(),
            private_key_pem=data['private_key'],
            certificate_jwt=data['certificate'],
            operator_url=operator_url,
        )
    except Exception as e:
        return jsonify({'error': 'invalid_identity',
                        'message': f'Could not load key/certificate: {e}'}), 400

    PROVIDER_DID    = data['provider_did'].strip()
    BASE_URL        = data['provider_base_url'].strip()
    OPERATOR_URL    = operator_url
    client          = new_client
    if (data.get('payment_market') or '').strip():
        PAYMENT_MARKET = data['payment_market'].strip()
    if (data.get('payment_network') or '').strip():
        PAYMENT_NETWORK = data['payment_network'].strip()
    if data.get('service_price') is not None:
        try:
            SERVICE_PRICE = int(data['service_price'])
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_service_price'}), 400

    os.environ['PROVIDER_DID']    = PROVIDER_DID
    os.environ['PAYMENT_MARKET']  = PAYMENT_MARKET
    os.environ['PAYMENT_NETWORK'] = PAYMENT_NETWORK
    if (data.get('nustro_principal_key') or '').strip():
        os.environ['NUSTRO_PRINCIPAL_KEY'] = data['nustro_principal_key'].strip()
    _payment_address_cache.clear()   # market/network may have changed

    return jsonify({
        'status':            'configured',
        'provider_did':      PROVIDER_DID,
        'provider_base_url': BASE_URL,
        'operator_url':      OPERATOR_URL,
        'payment_market':    PAYMENT_MARKET,
        'payment_network':   PAYMENT_NETWORK,
        'service_price':     SERVICE_PRICE,
        'principal_key_set': bool(os.environ.get('NUSTRO_PRINCIPAL_KEY')),
    }), 200

# ── Payment address cache ──────────────────────────────────────────────────────
# The NustroSettlement contract address and token address are stable between
# upgrades — we cache them to avoid a Operator call on every 402 response.
# The cache is invalidated on process restart, which is acceptable.
_payment_address_cache = {}


def _get_payment_address(consumer_did: str = None, amount_whole=None) -> dict | None:
    """
    Fetch the NustroSettlement contract address and ERC-20 token address
    from the Nustro Operator for this market/network combination.

    The contract/token details are cached in memory after the first fetch.
    When consumer_did is provided, a fresh request mints a payment intent —
    the Operator first enforces the consumer's spend policy at intent creation,
    so we must also send `amount` (the quoted price, in WHOLE currency units)
    and our own `provider_did` (for the consumer's counterparty-floor checks).

    Returns a dict with: contract, token, chain_id, currency, decimals
    and optionally intent_id, expires_at (when consumer_did is provided). On a
    spend-policy refusal the Operator answers 403; we return
    {'spend_refused': <detail>} so the caller relays the refusal to the
    consumer instead of a 402.

    On failure returns {'unavailable': <reason>} — a human-readable reason the
    caller surfaces to the consumer, instead of a bare None that says nothing.
    """
    market  = os.environ.get('PAYMENT_MARKET', PAYMENT_MARKET)
    network = os.environ.get('PAYMENT_NETWORK', PAYMENT_NETWORK)
    key     = os.environ.get('NUSTRO_PRINCIPAL_KEY', '')
    provider_did = os.environ.get('PROVIDER_DID', PROVIDER_DID)

    # Fail fast with a precise reason: without a principal key the Operator will
    # 401 every payment-address call, so no intent can ever be minted.
    if not key:
        return {'unavailable': 'No NUSTRO_PRINCIPAL_KEY configured on this Provider — '
                               'the Operator will reject the payment-intent request. '
                               'Set it via POST /configure or .env.'}

    cache_key = f"{market}:{network}"

    # Populate the cache if empty (first request after startup)
    if cache_key not in _payment_address_cache:
        try:
            resp = http_requests.get(
                f"{OPERATOR_URL}/v1/payment-address",
                params  = {'market': market, 'network': network},
                headers = {'Nustro-Principal-Key': key},
                timeout = 10,
            )
            print(f"[PAYMENT] payment-address fetch: status={resp.status_code}", flush=True)
            if resp.status_code == 200:
                _payment_address_cache[cache_key] = resp.json()
            else:
                print(f"[PAYMENT] fetch failed: {resp.text[:200]}", flush=True)
                try:
                    body = resp.json()
                    msg = body.get('message') or body.get('error') or resp.text[:120]
                except Exception:
                    msg = resp.text[:120]
                return {'unavailable': f"Operator rejected the payment-address request "
                                       f"({resp.status_code}) for {market} on {network}: {msg}"}
        except Exception as e:
            print(f"[PAYMENT] fetch exception: {e}", flush=True)
            return {'unavailable': f"Operator unreachable at {OPERATOR_URL}: {e}"}

    base = _payment_address_cache.get(cache_key)
    if not base:
        return {'unavailable': f"No payment configuration for {market} on {network}."}

    # If consumer_did is provided, make a fresh request to auto-create a
    # payment_intent on the Operator. The intent expires in 5 minutes.
    # If not paid within that window, it's marked ABANDONED by the Operator.
    if consumer_did:
        try:
            resp = http_requests.get(
                f"{OPERATOR_URL}/v1/payment-address",
                params  = {'market': market, 'network': network,
                           'consumer_did': consumer_did,
                           'amount': (str(amount_whole) if amount_whole is not None else None),
                           'provider_did': provider_did},
                headers = {'Nustro-Principal-Key': key},
                timeout = 10,
            )
            if resp.status_code == 200:
                return resp.json()  # Includes intent_id + expires_at
            if resp.status_code == 403:
                # Operator refused the intent on the consumer's spend policy.
                try:
                    detail = resp.json().get('detail') or {}
                except Exception:
                    detail = {}
                print(f"[PAYMENT] intent refused (spend policy): {detail}", flush=True)
                return {'spend_refused': detail}
        except Exception as e:
            print(f"[PAYMENT] intent creation failed: {e}", flush=True)
            # Fall through to cached data — intent creation is optional

    return base


def _get_provider_did_hash() -> str:
    """
    Compute keccak256(provider_did) — required by NustroSettlement.pay().

    The Consumer includes this in their on-chain pay() call so the
    NustroSettlement contract can look up the Provider's registered wallets
    (operational, escrow) and split the payment correctly.
    """
    from web3 import Web3
    return Web3.keccak(text=PROVIDER_DID).hex()


# ── Discovery endpoints ────────────────────────────────────────────────────────
# These are the AEA/P standard well-known endpoints. Every AEA/P Provider
# MUST implement them. Consumers call these before sending any request.

@app.route('/.well-known/aeap', methods=['GET'])
def discovery():
    """
    Phase 1 — Discovery document.

    Returns the Provider's AID (Agent Identity Document) containing:
      - The Provider's DID and certificate
      - Where to send AEA/P challenges (Phase 1)
      - The Provider's economic role, capabilities, and escrow state

    Consumers fetch this to verify they're talking to a legitimate
    AEA/P-certified agent before initiating payment.
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
    The Consumer verifies the signature offline (no Operator call needed).

    This proves the Provider controls the private key matching the
    public key in its AEA/P certificate — i.e., it is who it claims to be.

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
        'certificate':        headers['AEAP-Certificate'],
        'challenge_response': headers['AEAP-Challenge-Response'],
        'timestamp':          headers['AEAP-Timestamp'],
        'agent_id':           PROVIDER_DID,
    })


# ── Service endpoint ───────────────────────────────────────────────────────────

@app.route('/research', methods=['GET'])
def research_payment_required():
    """
    Phase 2 — Return 402 Payment Required with payment instructions.

    The Consumer calls GET /research first to discover:
      - How much to pay (amount in token base units)
      - Which contract to call (NustroSettlement address)
      - Which token to use (USDC contract address)
      - The Provider's DID hash (required by NustroSettlement.pay())
      - When the quote expires (Consumer must pay before this time)

    The Consumer then:
      1. Approves the ERC-20 token spend (token.approve)
      2. Calls NustroSettlement.pay() on-chain
      3. Calls POST /research with the tx_hash as proof

    IMPORTANT: We return the Provider DID hash, NOT a wallet address.
    The Consumer pays the NustroSettlement contract — it handles routing
    funds to the Provider's operational wallet, escrow wallet, and
    the Nustro fee wallet atomically. The Provider never receives raw payments.

    If consumer_did is passed as a query param, a payment intent is created
    on the Operator so Consumer abandonment can be tracked for PoP rating.
    """
    # Consumer passes their DID so the Operator can spend-check + mint an intent.
    consumer_did = request.args.get('consumer_did', '') or None

    # Quoted price in WHOLE currency units (the Operator enforces the consumer's
    # spend policy against this). SERVICE_PRICE is in token base units.
    decimals    = 6
    amount_whole = SERVICE_PRICE / (10 ** decimals)

    payment_addr = _get_payment_address(consumer_did=consumer_did, amount_whole=amount_whole)
    if not payment_addr:
        return jsonify({
            'error':   'payment_config_unavailable',
            'message': 'Provider payment configuration is unavailable.',
        }), 503

    # Couldn't obtain a settlement target / intent — say exactly why, rather
    # than implying a transient outage the caller should retry.
    if payment_addr.get('unavailable'):
        return jsonify({
            'error':   'payment_config_unavailable',
            'message': payment_addr['unavailable'],
        }), 503

    # The Operator refused the intent on the consumer's spend policy — relay it
    # to the consumer (no 402; the consumer's principal must widen its scope).
    if payment_addr.get('spend_refused') is not None:
        return jsonify({
            'error':   'spend_policy_violation',
            'message': 'The Operator refused this payment under the consumer spend policy.',
            'detail':  payment_addr['spend_refused'],
        }), 403

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

    PHASE 4a — Verify Consumer identity (AEA/P bound proof)
      The Consumer includes AEAP-Certificate and AEAP-Proof headers.
      We verify the certificate was signed by the Nustro CA and the proof
      signature binds the Consumer's DID to this Provider's DID.
      This is done OFFLINE — no Operator call needed for verification.

    PHASE 4b — Verify payment proof header
      The Consumer includes AEAP-Payment-Tx: { tx_hash, network }.
      This is NOT on-chain verification — that happens in Phase 5.

    PHASE 5 — Facilitation (Operator verifies payment on-chain)
      We call POST /v1/facilitate with the tx_hash. The Nustro Operator:
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
      AEAP-Certificate    — Consumer's AEA/P certificate JWT
      AEAP-Proof          — EC signature: timestamp|caller_did|callee_did
      AEAP-Timestamp      — ISO 8601 timestamp (must be within 30 seconds)
      AEAP-Payment-Tx     — JSON: { "tx_hash": "0x...", "network": "..." }

    Request body: { "query": "..." }
    """
    # Phase 4a: Verify Consumer AEA/P identity
    # verify_incoming() checks the certificate JWT signature against the
    # Nustro CA public key and verifies the bound proof EC signature.
    # Both are done offline — no Operator round-trip needed.
    verification = client.verify_incoming(
        request_headers = request.headers,
        own_did         = PROVIDER_DID,
    )

    if not verification['verified']:
        reason = verification.get('reason', 'verification_failed')
        if reason == 'missing_headers':
            # Consumer didn't include AEA/P headers — they need to pay first
            return jsonify({
                'error':   'certificate_required',
                'message': 'AEA/P certification required. GET /research for payment instructions.',
            }), 401
        return jsonify({'error': 'aeap_verification_failed', 'reason': reason}), 401

    consumer_did = verification['caller_did']
    print(f"[AUTH] Consumer verified: {consumer_did}", flush=True)

    # Phase 4b: Extract payment proof from header
    # The Consumer provides the tx_hash from their NustroSettlement.pay() call.
    # We don't verify it here — the Operator does that in Phase 5.
    payment_tx_header = request.headers.get('AEAP-Payment-Tx')
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
                'message': 'AEAP-Payment-Tx must be valid JSON: { "tx_hash": "0x...", "network": "..." }',
            }), 400

    if not tx_hash:
        return jsonify({
            'error':   'payment_required',
            'message': 'Payment required. GET /research for payment instructions, then include AEAP-Payment-Tx.',
        }), 402

    # Phase 5: Facilitation
    # Tell the Nustro Operator about the payment. It reads the blockchain
    # event, verifies correctness, and creates the PoP task record.
    # This is the only Operator call in the service flow.
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
        'consumer_agent_rating': verification.get('agent_rating'),
        'verified':             True,
        'facilitation_id':      facilitation.get('facilitation_id'),
        'task_id':              facilitation.get('task_id'),
    })


def _facilitate(consumer_did: str, tx_hash: str, network: str) -> dict:
    """
    POST /v1/facilitate — tell the Nustro Operator a payment occurred.

    The Operator reads the NustroSettlement Settled event from the blockchain,
    verifies all amounts and DID hashes, updates the Provider's escrow
    balance, and creates a PoP task record for the interaction.

    This is the ONLY call where we send the tx_hash to the Operator.
    The Operator never submits any transactions — it is always read-only.

    Returns a dict with success flag and facilitation details including
    task_id (None if same-principal — Sybil check prevents PoP gaming).
    """
    principal_key = os.environ.get('NUSTRO_PRINCIPAL_KEY', '')
    try:
        resp = http_requests.post(
            f"{OPERATOR_URL}/v1/facilitate",
            headers = {
                'Nustro-Principal-Key': principal_key,
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
        f"This is the Nustro reference Provider — payment verified via NustroSettlement. "
        f"In production this would call an LLM, database, or external API."
    )


# ── Health check ───────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """
    Health check endpoint.

    Returns the Provider's current status from the Nustro Operator,
    including escrow state and PoP rating. Useful for monitoring
    and debugging.

    settlement_contract will show 'not cached' until the first
    GET /research call populates the payment address cache.
    """
    if client is None:
        return jsonify({'status': 'unconfigured', 'role': 'PROVIDER',
                        'message': 'POST /configure to load an agent identity.'})
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
        # Without this the Operator 401s every payment-intent request, so the
        # run dies at the 402 step — surface it as a readiness signal.
        'principal_key_set':   bool(os.environ.get('NUSTRO_PRINCIPAL_KEY', '')),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
