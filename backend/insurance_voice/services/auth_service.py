"""Login + caller identity.

An auth surface that skips the API-key guard, a service that owns
password verification and token minting, and a dependency that turns a
bearer token back into a user. Deliberately minimal: sha256
password hashes and a stateless HMAC-signed access token - no passlib,
no JWT dependency, no refresh-token machinery.
"""

import base64
import hashlib
import hmac
import json
import time

from insurance_voice.db.models import UserInfo
from insurance_voice.db.session import Database


class AuthError(Exception):
    """Invalid credentials or invalid/expired token."""


# The one seeded caller. Static contractual facts only - anything dynamic
# (billing, networks, scores) stays behind the mock external-system tools.
DEMO_USER_DATA = {
    "name": "Bibek Pal",
    "email": "bibek@example.com",
    "phone": "+91-9000000001",
    "city": "Mumbai",
    "policies": [
        {
            "policy_number": "POL-1001",
            "plan": "Family Floater Gold",
            "coverage_type": "cashless_and_reimbursement",
            "sum_insured": 500000,
            "active": True,
            "required_claim_documents": ["discharge_summary", "bills", "id_proof"],
        }
    ],
}


class AuthService:
    def __init__(self, *, db: Database, secret_key: str, token_ttl_s: int = 7 * 24 * 3600):
        self.db = db
        self._secret = secret_key.encode()
        self.token_ttl_s = token_ttl_s

    # ---- password + token primitives -------------------------------------
    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def create_token(self, user_id: str) -> str:
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": user_id, "exp": int(time.time()) + self.token_ttl_s}).encode()
        ).decode()
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify_token(self, token: str) -> str:
        """Return the user_id inside a valid, unexpired token; raise AuthError otherwise."""
        try:
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise AuthError("Invalid token signature")
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except (ValueError, TypeError) as err:
            raise AuthError("Malformed token") from err
        if claims.get("exp", 0) < time.time():
            raise AuthError("Token expired")
        return claims["sub"]

    # ---- operations -------------------------------------------------------
    async def login(self, user_id: str, password: str) -> dict:
        async with self.db.session() as s:
            row = await s.get(UserInfo, user_id)
        if row is None or not hmac.compare_digest(row.password_hash, self.hash_password(password)):
            raise AuthError("Invalid user id or password")
        return {"access_token": self.create_token(row.user_id), "user_id": row.user_id, "user_data": row.user_data}

    async def get_user_data(self, user_id: str) -> dict | None:
        async with self.db.session() as s:
            row = await s.get(UserInfo, user_id)
        return row.user_data if row is not None else None

    async def seed_demo_user(self, user_id: str, password: str) -> None:
        """Idempotent startup seed: insert the demo caller, or refresh the
        profile/credentials if the row already exists (keeps redeploys
        deterministic)."""
        async with self.db.session() as s:
            row = await s.get(UserInfo, user_id)
            if row is None:
                s.add(UserInfo(user_id=user_id, password_hash=self.hash_password(password), user_data=DEMO_USER_DATA))
            else:
                row.password_hash = self.hash_password(password)
                row.user_data = DEMO_USER_DATA
            await s.commit()
