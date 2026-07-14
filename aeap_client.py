"""
AEAP Client Library — Reference Implementation

This is the shared library used by both PAgent and CAgent in the
AEAP reference implementation. It wraps the full mutual authentication
flow into clean, reusable function calls.

Developers integrating AEAP into their own agents can use this as a
starting point — copy it, adapt it, or use it as-is.

Usage:

  from aeap_client import AEAPClient

  client = AEAPClient(
      agent_did='did:aeap:...',
      private_key_path='/path/to/private_key.pem',
      certificate_path='/path/to/certificate.jwt',
      operator_url='https://api.nustro.ai'
  )

  # Phase 1 — verify a counterparty before calling
  result = client.verify_counterparty('did:aeap:...')
  if not result['verified']:
      raise Exception(result['reason'])

  # Phase 2 — get headers to present when calling a counterparty
  headers = client.get_auth_headers(callee_did='did:aeap:...')
  response = requests.post(url, headers=headers, json=payload)

  # Verify an incoming call (Phase 2, server side)
  result = client.verify_incoming(request_headers, own_did='did:aeap:...')
"""

import os
import base64
import secrets
from datetime import datetime, timezone

import jwt
import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature


# Operator base URL. Defaults to Nustro; override per-operator.
OPERATOR_URL = os.environ.get('OPERATOR_URL', 'https://api.nustro.ai')
TIMESTAMP_WINDOW_SECONDS = 30
STATUS_CACHE_TTL = 30  # seconds


class AEAPClient:

    def __init__(self, agent_did, private_key_path, certificate_path,
                 operator_url=OPERATOR_URL):
        self.agent_did = agent_did
        self.operator_url = operator_url

        # Load private key
        with open(private_key_path, 'rb') as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )

        # Load certificate JWT
        with open(certificate_path, 'r') as f:
            self.certificate_jwt = f.read().strip()

        # Simple in-memory status cache
        self._status_cache = {}

    # ── Key operations ──────────────────────────────────────────────────────

    def sign(self, message: str) -> str:
        """Sign a message string with the agent's private key. Returns base64."""
        signature = self.private_key.sign(
            message.encode('utf-8'),
            ec.ECDSA(hashes.SHA256())
        )
        return base64.b64encode(signature).decode('ascii')

    def _timestamp(self) -> str:
        """Current UTC timestamp in ISO 8601 format."""
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ── Phase 1 — Caller verifies callee ────────────────────────────────────

    def verify_counterparty(self, counterparty_did: str,
                             require_environment: str = None,
                             require_min_pop: float = None) -> dict:
        """
        Phase 1 verification — verify a counterparty before calling them.

        Steps:
          1. Get a challenge nonce from the Operator
          2. Request the counterparty's certificate and signed response
             (caller does this by including the nonce in the request header)
          3. Verify certificate against AEAP CA
          4. Verify EC signature
          5. Check status endpoint

        In the reference implementation, steps 2-4 are handled by
        verify_certificate_and_response() after the counterparty responds.

        This method handles step 1 and step 5 (the Operator calls).
        Steps 2-4 are offline and handled in verify_certificate_and_response().

        Args:
            counterparty_did: DID of the agent to verify
            require_environment: 'production' to reject sandbox agents
            require_min_pop: minimum agent_rating to accept (None = accept any)

        Returns dict with:
            verified: bool
            reason: error code if not verified
            status: the status response if verified
        """
        # Step 1 — get nonce
        try:
            nonce_resp = requests.get(
                f"{self.operator_url}/v1/verify/challenge",
                timeout=5
            )
            nonce = nonce_resp.json()['nonce']
        except Exception as e:
            return {'verified': False, 'reason': 'platform_unreachable', 'error': str(e)}

        # Step 5 — check status (steps 2-4 happen via request/response headers)
        status = self._get_status(counterparty_did)
        if not status:
            return {'verified': False, 'reason': 'status_check_failed'}

        if status.get('status') != 'ACTIVE':
            return {'verified': False, 'reason': f"agent_{status.get('status', 'unknown').lower()}"}

        if require_environment and status.get('environment') != require_environment:
            return {
                'verified': False,
                'reason': 'environment_not_accepted',
                'expected': require_environment,
                'actual': status.get('environment')
            }

        if require_min_pop is not None:
            pop = status.get('agent_rating')
            if pop is None or pop < require_min_pop:
                return {
                    'verified': False,
                    'reason': 'rating_below_threshold',
                    'agent_rating': pop,
                    'required': require_min_pop
                }

        escrow_state = status.get('escrow_state')
        if escrow_state == 'CONSTRAINED':
            return {'verified': False, 'reason': 'escrow_constrained'}

        return {
            'verified': True,
            'nonce': nonce,
            'status': status,
        }

    def verify_certificate_and_response(self, certificate_jwt: str,
                                         challenge_response: str,
                                         timestamp: str,
                                         nonce: str) -> dict:
        """
        Verify a counterparty's certificate and challenge response offline.
        Called after the counterparty responds to our challenge.

        Args:
            certificate_jwt: counterparty's AEAP certificate
            challenge_response: base64 EC signature
            timestamp: ISO 8601 timestamp from response
            nonce: the nonce we sent in the challenge

        Returns dict with verified: bool, reason, and claims if verified.
        """
        # Verify certificate via the Operator (could also be done locally
        # with CA public key — Operator endpoint used here for simplicity)
        try:
            cert_resp = requests.post(
                f"{self.operator_url}/v1/verify/certificate",
                json={'certificate': certificate_jwt},
                timeout=5
            )
            cert_data = cert_resp.json()
        except Exception as e:
            return {'verified': False, 'reason': 'platform_unreachable', 'error': str(e)}

        if not cert_data.get('valid'):
            return {'verified': False, 'reason': cert_data.get('reason', 'invalid_certificate')}

        # Verify challenge response via the Operator
        try:
            proof_resp = requests.post(
                f"{self.operator_url}/v1/verify/proof",
                json={
                    'proof_type': 'challenge_response',
                    'certificate': certificate_jwt,
                    'nonce': nonce,
                    'proof': challenge_response,
                    'timestamp': timestamp,
                },
                timeout=5
            )
            proof_data = proof_resp.json()
        except Exception as e:
            return {'verified': False, 'reason': 'platform_unreachable', 'error': str(e)}

        if not proof_data.get('verified'):
            return {'verified': False, 'reason': proof_data.get('reason', 'invalid_proof')}

        return {
            'verified': True,
            'agent_id': cert_data.get('agent_id'),
            'cert_tier': cert_data.get('cert_tier'),
            'economic_role': cert_data.get('economic_role'),
            'claims': cert_data.get('claims', {}),
        }

    # ── Phase 2 — Callee verifies caller ────────────────────────────────────

    def get_auth_headers(self, callee_did: str) -> dict:
        """
        Generate AEAP authentication headers for an outgoing request.
        These headers prove identity to the callee (Phase 2).

        The bound proof signs timestamp|caller_did|callee_did — binding
        the callee DID into the signature makes it non-replayable against
        any other agent.

        Returns headers dict ready to pass to requests.
        """
        timestamp = self._timestamp()
        message = f"{timestamp}|{self.agent_did}|{callee_did}"
        proof = self.sign(message)

        return {
            'AEAP-Certificate': self.certificate_jwt,
            'AEAP-Proof': proof,
            'AEAP-Timestamp': timestamp,
        }

    def get_challenge_response_headers(self, nonce: str) -> dict:
        """
        Generate headers responding to a challenge (Phase 1, server side).
        Called when a counterparty has sent us AEAP-Challenge.

        Returns headers dict to include in the response.
        """
        timestamp = self._timestamp()
        message = f"{nonce}|{self.agent_did}|{timestamp}"
        response_sig = self.sign(message)

        return {
            'AEAP-Certificate': self.certificate_jwt,
            'AEAP-Challenge-Response': response_sig,
            'AEAP-Timestamp': timestamp,
        }

    def verify_incoming(self, request_headers: dict, own_did: str) -> dict:
        """
        Verify an incoming request from a caller (Phase 2, server side).

        Checks AEAP-Certificate + AEAP-Proof + AEAP-Timestamp headers.
        Verifies the bound proof contains our own DID.

        Args:
            request_headers: dict of incoming request headers
            own_did: this agent's DID (must appear in the proof)

        Returns dict with verified: bool, reason, caller info if verified.
        """
        certificate = request_headers.get('AEAP-Certificate')
        proof = request_headers.get('AEAP-Proof')
        timestamp = request_headers.get('AEAP-Timestamp')

        if not certificate or not proof or not timestamp:
            return {
                'verified': False,
                'reason': 'missing_headers',
                'message': (
                    'AEAP-Certificate, AEAP-Proof, and '
                    'AEAP-Timestamp headers required'
                ),
                'enrollment_url': 'https://api.nustro.ai/v1/enroll',
            }

        # Verify certificate
        try:
            cert_resp = requests.post(
                f"{self.operator_url}/v1/verify/certificate",
                json={'certificate': certificate},
                timeout=5
            )
            cert_data = cert_resp.json()
        except Exception as e:
            return {'verified': False, 'reason': 'platform_unreachable', 'error': str(e)}

        if not cert_data.get('valid'):
            return {'verified': False, 'reason': cert_data.get('reason', 'invalid_certificate')}

        caller_did = cert_data.get('agent_id')

        # Verify bound proof
        try:
            proof_resp = requests.post(
                f"{self.operator_url}/v1/verify/proof",
                json={
                    'proof_type': 'bound_proof',
                    'certificate': certificate,
                    'proof': proof,
                    'timestamp': timestamp,
                    'callee_did': own_did,
                },
                timeout=5
            )
            proof_data = proof_resp.json()
        except Exception as e:
            return {'verified': False, 'reason': 'platform_unreachable', 'error': str(e)}

        if not proof_data.get('verified'):
            return {'verified': False, 'reason': proof_data.get('reason', 'invalid_proof')}

        # Check caller status
        status = self._get_status(caller_did)
        if not status:
            return {'verified': False, 'reason': 'status_check_failed'}

        if status.get('status') != 'ACTIVE':
            return {'verified': False, 'reason': f"caller_{status.get('status', 'unknown').lower()}"}

        return {
            'verified': True,
            'caller_did': caller_did,
            'cert_tier': cert_data.get('cert_tier'),
            'economic_role': cert_data.get('economic_role'),
            'environment': status.get('environment'),
            'agent_rating': status.get('agent_rating'),
        }

    # ── Status endpoint ──────────────────────────────────────────────────────

    def _get_status(self, agent_did: str) -> dict:
        """
        Fetch agent status from the Operator. Simple in-memory cache with TTL.
        In production, use Redis or similar for shared cache across workers.
        """
        import time
        now = time.time()

        cached = self._status_cache.get(agent_did)
        if cached and (now - cached['fetched_at']) < STATUS_CACHE_TTL:
            return cached['data']

        try:
            resp = requests.get(
                f"{self.operator_url}/v1/agents/{agent_did}/status",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                self._status_cache[agent_did] = {
                    'data': data,
                    'fetched_at': now
                }
                return data
        except Exception:
            pass

        return None

    def get_own_status(self) -> dict:
        """Fetch this agent's own current status."""
        return self._get_status(self.agent_did)

    # ── Discovery document ───────────────────────────────────────────────────

    def discovery_document(self, challenge_endpoint: str) -> dict:
        """
        Generate the .well-known/aeap discovery document for this agent.
        Counterparties fetch this to initiate the challenge flow.
        """
        cert_claims = jwt.decode(
            self.certificate_jwt,
            options={'verify_signature': False}
        )
        aeap_claims = cert_claims.get('aeap', {})

        return {
            'agent_id': self.agent_did,
            'aid_url': f"{self.operator_url}/v1/agents/{self.agent_did}/aid",
            'certificate': self.certificate_jwt,
            'economic_role': aeap_claims.get('economic_role'),
            'capabilities': aeap_claims.get('capabilities', []),
            'authorized_actions': aeap_claims.get('authorized_actions', []),
            'challenge_endpoint': challenge_endpoint,
        }
