"""Mock insurance tools.

Simulated external systems with a small in-memory fixture dataset.
Latency/failure/retry behavior lives in the registry wrapper (base.py),
so each tool body is just the business payload.
"""

import hashlib
import typing as t

from insurance_voice.services.tools.base import ToolPolicy, ToolRegistry


# session_id -> caller profile dict (or None when not logged in). Wired by
# the factory to a user_info lookup; tools stay import-clean of the DB.
ProfileLookup = t.Callable[[str], t.Awaitable[dict | None]]
# session_id -> the calling user's claims across all their conversations.
ClaimsLookup = t.Callable[[str], t.Awaitable[list[dict]]]

_POLICIES = {
    "POL-1001": {
        "holder": "Bibek Pal",  # the seeded demo caller owns this policy
        "plan": "Family Floater Gold",
        "sum_insured": 500000,
        "coverage_type": "cashless_and_reimbursement",
        "active": True,
    },
    "POL-2002": {
        "holder": "Rahul Nair",
        "plan": "Individual Silver",
        "sum_insured": 200000,
        "coverage_type": "reimbursement_only",
        "active": True,
    },
    "POL-3003": {
        "holder": "Meera Iyer",
        "plan": "Senior Care",
        "sum_insured": 300000,
        "coverage_type": "cashless_and_reimbursement",
        "active": False,
    },
}

_HOSPITAL_NETWORK = {
    "mumbai": ["Lilavati Hospital", "Kokilaben Hospital", "Fortis Mulund"],
    "delhi": ["AIIMS Delhi", "Max Saket", "Apollo Indraprastha"],
    "bangalore": ["Manipal Whitefield", "Fortis Bannerghatta", "Narayana Health City"],
}

_BILLING = {
    "POL-1001": {"premium_due": 0, "next_due_date": "2026-09-01", "status": "paid"},
    "POL-2002": {"premium_due": 8450, "next_due_date": "2026-07-15", "status": "due"},
    "POL-3003": {"premium_due": 12000, "next_due_date": "2026-05-01", "status": "lapsed"},
}

REQUIRED_CLAIM_DOCUMENTS = ["discharge_summary", "bills", "id_proof"]


def build_default_registry(
    policy: ToolPolicy,
    profile_lookup: ProfileLookup | None = None,
    claims_lookup: ClaimsLookup | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(policy=policy)

    if claims_lookup is not None:

        @registry.tool(
            "get_claim_status",
            description=(
                "Check the status of claims the caller has already filed - queued for human review, "
                "approved, rejected, or submitted with a reference number - across all their "
                "conversations. Use this when the caller asks about an existing claim. NEVER re-file "
                "or re-queue a claim that this tool shows as pending."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )
        async def get_claim_status(_session_id: str = "") -> dict:
            claims = await claims_lookup(_session_id)
            return {"claims": claims, "count": len(claims)}

    if profile_lookup is not None:

        @registry.tool(
            "get_caller_profile",
            description=(
                "Fetch the logged-in caller's profile: name, city, and the policies they hold "
                "(number, plan, coverage type, sum insured, required claim documents). Call this "
                "before asking the caller for their policy number or city - they may have already "
                "identified themselves at login."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )
        async def get_caller_profile(_session_id: str = "") -> dict:
            profile = await profile_lookup(_session_id)
            if profile is None:
                return {"found": False, "note": "Caller is not logged in - ask for details directly."}
            return {"found": True, "profile": profile}

    @registry.tool(
        "get_policy_details",
        description="Fetch policy plan, coverage type (cashless vs reimbursement) and sum insured for a policy number.",
        parameters={
            "type": "object",
            "properties": {"policy_number": {"type": "string", "description": "e.g. POL-1001"}},
            "required": ["policy_number"],
        },
    )
    async def get_policy_details(policy_number: str) -> dict:
        policy_record = _POLICIES.get(policy_number.upper())
        if policy_record is None:
            return {"found": False, "policy_number": policy_number}
        return {"found": True, "policy_number": policy_number.upper(), **policy_record}

    @registry.tool(
        "get_hospital_network",
        description="List cashless network hospitals in a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    async def get_hospital_network(city: str) -> dict:
        hospitals = _HOSPITAL_NETWORK.get(city.strip().lower(), [])
        return {"city": city, "hospitals": hospitals, "in_network_count": len(hospitals)}

    @registry.tool(
        "get_billing_status",
        description="Fetch premium due amount, next due date and billing status for a policy number.",
        parameters={
            "type": "object",
            "properties": {"policy_number": {"type": "string"}},
            "required": ["policy_number"],
        },
    )
    async def get_billing_status(policy_number: str) -> dict:
        billing = _BILLING.get(policy_number.upper())
        if billing is None:
            return {"found": False, "policy_number": policy_number}
        return {"found": True, "policy_number": policy_number.upper(), **billing}

    @registry.tool(
        "calculate_claim_probability",
        description=(
            "Compute the claim acceptance probability score from policy number, claim amount and the "
            "list of documents gathered so far."
        ),
        parameters={
            "type": "object",
            "properties": {
                "policy_number": {"type": "string"},
                "claim_amount": {"type": "number"},
                "documents": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["policy_number", "claim_amount", "documents"],
        },
    )
    async def calculate_claim_probability(policy_number: str, claim_amount: float, documents: list) -> dict:
        policy_record = _POLICIES.get(policy_number.upper())
        # Deterministic pseudo-model: document completeness + amount-vs-cover
        # ratio + a stable hash jitter so identical inputs always score the same.
        doc_score = len(set(documents) & set(REQUIRED_CLAIM_DOCUMENTS)) / len(REQUIRED_CLAIM_DOCUMENTS)
        if policy_record is None or not policy_record["active"]:
            base = 0.05
            amount_score = 0.0
        else:
            base = 0.35
            amount_score = max(0.0, 1.0 - (claim_amount / policy_record["sum_insured"])) * 0.25
        jitter_seed = f"{policy_number}:{claim_amount}:{sorted(documents)}"
        jitter = int(hashlib.sha256(jitter_seed.encode()).hexdigest(), 16) % 100 / 1000  # 0.000-0.099
        probability = round(min(0.99, base + doc_score * 0.35 + amount_score + jitter), 3)
        missing = sorted(set(REQUIRED_CLAIM_DOCUMENTS) - set(documents))
        return {"probability": probability, "missing_documents": missing}

    @registry.tool(
        "submit_claim",
        description=(
            "Submit the final claim. Only callable after explicit human approval - never call this "
            "directly from conversation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "policy_number": {"type": "string"},
                "claim_amount": {"type": "number"},
                "documents": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["policy_number", "claim_amount", "documents"],
        },
    )
    async def submit_claim(policy_number: str, claim_amount: float, documents: list) -> dict:
        reference = "CLM-" + hashlib.sha256(f"{policy_number}:{claim_amount}".encode()).hexdigest()[:8].upper()
        return {"submitted": True, "reference": reference, "policy_number": policy_number.upper()}

    return registry
