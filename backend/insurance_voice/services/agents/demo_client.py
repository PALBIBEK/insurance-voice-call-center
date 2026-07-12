"""Zero-key demo chat client.

Stands in for the real LLM when no OpenRouter key is configured, so the
whole stack (frontend included) is demoable offline. Mimics the exact
chat.completions surface the runtime uses and drives the same tools,
handoffs and HITL flow - just with keyword rules instead of a model.
Replaced by AsyncOpenAI(base_url=OpenRouter) the moment a key exists;
nothing else in the system changes.
"""

import json
import re
import types


def _text(content: str):
    message = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _tool(name: str, args: dict):
    call = types.SimpleNamespace(
        id="call_demo",
        type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    message = types.SimpleNamespace(content=None, tool_calls=[call])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


_POLICY_RE = re.compile(r"\bPOL-\d+\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\b(\d{4,9})\b")
_CITY_RE = re.compile(r"\b(mumbai|delhi|bangalore)\b", re.IGNORECASE)
_DOCS = ["discharge_summary", "bills", "id_proof"]


def _conversation_text(messages: list[dict]) -> str:
    return " ".join(str(m.get("content") or "") for m in messages if m.get("role") in ("user", "assistant"))


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _flatten_profile(profile: dict) -> str:
    """Profile -> routing text. Deliberately omits numeric fields (sum
    insured, phone) so the claims amount-regex never mistakes them for a
    claim amount; the real LLM sees the full tool payload instead."""
    parts = [str(profile.get("name", "")), str(profile.get("city", ""))]
    for policy in profile.get("policies", []):
        parts.append(f"{policy.get('policy_number', '')} {policy.get('plan', '')} {policy.get('coverage_type', '')}")
    return " ".join(p for p in parts if p)


def _pending_tool_result(messages: list[dict]) -> dict | None:
    if messages and messages[-1].get("role") == "tool":
        try:
            return json.loads(messages[-1]["content"])
        except (ValueError, TypeError):
            return None
    return None


class DemoChatClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def _create(self, *, model: str, messages: list[dict], tools: list[dict]):
        available = {t["function"]["name"] for t in tools}
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = _last_user(messages).lower()
        everything = _conversation_text(messages)
        profile_fetched = False

        tool_result = _pending_tool_result(messages)
        if tool_result is not None:
            # Profile result: not an answer in itself - enrich the routing
            # context (policy number, city) and decide the next step with it.
            if "profile" in tool_result or "not logged in" in str(tool_result.get("note", "")):
                everything += " " + _flatten_profile(tool_result.get("profile") or {})
                profile_fetched = True
            else:
                return self._summarize_tool_result(tool_result)

        if "triage agent" in system:
            return self._triage(user, available)
        if "claims specialist" in system:
            return self._claims(user, everything, available, profile_fetched)
        return self._policy(user, everything, available, profile_fetched)

    @staticmethod
    def _triage(user: str, available: set[str]):
        if any(w in user for w in ("claim", "document", "submit", "hospitalized", "accident", "surgery")):
            return _tool("handoff_to_claims", {"reason": "caller wants to work on a claim"})
        if any(w in user for w in ("policy", "billing", "premium", "cashless", "reimburs", "hospital", "cover", "due")):
            return _tool("handoff_to_policy", {"reason": "policy or billing question"})
        return _text("I can help with your policy, billing, or claims. Which one do you need today?")

    @staticmethod
    def _policy(user: str, everything: str, available: set[str], profile_fetched: bool = False):
        policy_numbers = _POLICY_RE.findall(everything)
        can_fetch_profile = "get_caller_profile" in available and not profile_fetched
        # claim intent outranks everything ("I was hospitalized and want to
        # file a claim" must hand off, not match the hospital-network rule)
        if any(w in user for w in ("claim", "file a", "submit")) and "handoff_to_claims" in available:
            return _tool("handoff_to_claims", {"reason": "caller wants to file a claim"})
        if re.search(r"\bhospitals?\b", user) or "cashless" in user or "network" in user:
            # city may come from the message itself or from the caller's profile
            city_match = _CITY_RE.search(user) or _CITY_RE.search(everything)
            if city_match and "get_hospital_network" in available:
                return _tool("get_hospital_network", {"city": city_match.group(1).lower()})
            if can_fetch_profile:
                return _tool("get_caller_profile", {})
            if "get_hospital_network" in available and not city_match:
                return _text("Which city should I check the cashless hospital network for?")
        if not policy_numbers:
            if can_fetch_profile:
                return _tool("get_caller_profile", {})
            return _text("Sure - could you share your policy number? It looks like POL-1001.")
        if any(w in user for w in ("billing", "premium", "due", "owe", "paid")):
            return _tool("get_billing_status", {"policy_number": policy_numbers[-1].upper()})
        return _tool("get_policy_details", {"policy_number": policy_numbers[-1].upper()})

    @staticmethod
    def _claims(user: str, everything: str, available: set[str], profile_fetched: bool = False):
        # Off-domain hand-back: a billing/coverage question must leave the
        # claims flow on THIS turn - answering it with claim data is the bug
        # this rule prevents. Guarded on claim-intent words so sentences like
        # "I was hospitalized and want to file a claim" never bounce back.
        billing_words = ("premium", "billing", "owe", "due", "paid", "cashless", "network", "hospitals", "reimburs", "coverage", "what does my policy cover")
        claim_words = ("claim", "submit", "file", "document", "discharge")
        if (
            "handoff_to_policy" in available
            and any(w in user for w in billing_words)
            and not any(w in user for w in claim_words)
        ):
            return _tool("handoff_to_policy", {"reason": "billing or coverage question during claims flow"})

        # Existing-claim status question: read, never re-file.
        status_words = ("what about", "status", "any update", "update on", "approved yet", "happened to")
        if "get_claim_status" in available and any(w in user for w in status_words):
            return _tool("get_claim_status", {})

        policy_numbers = _POLICY_RE.findall(everything)
        if not policy_numbers:
            if "get_caller_profile" in available and not profile_fetched:
                return _tool("get_caller_profile", {})
            return _text("I can help with your claim. First, what's your policy number?")
        amounts = [a for a in _AMOUNT_RE.findall(everything) if f"POL-{a}" not in everything.upper()]
        if not amounts:
            return _text("What is the claim amount in rupees?")
        docs_present = [d for d in _DOCS if d.replace("_", " ") in everything.lower() or d in everything.lower()]
        wants_submit = any(w in user for w in ("submit", "go ahead", "confirm", "yes please", "proceed"))
        args = {
            "policy_number": policy_numbers[-1].upper(),  # most recent mention wins
            "claim_amount": float(amounts[-1]),
            "documents": docs_present,
        }
        if wants_submit and "request_claim_approval" in available:
            # carry the last spoken probability into the approval card
            prob_match = re.findall(r"(\d{1,2}) percent", everything)
            if prob_match:
                args["probability"] = int(prob_match[-1]) / 100
            return _tool("request_claim_approval", args)
        return _tool("calculate_claim_probability", args)

    @staticmethod
    def _summarize_tool_result(result: dict):
        if "claims" in result:
            claims = result["claims"]
            if not claims:
                return _text("I don't see any claims on file for you. Would you like to start one?")
            latest = claims[0]
            state = {
                "pending": "queued for human review - you'll hear back once a reviewer decides",
                "approved": f"approved and submitted (reference {latest.get('submitted_reference')})",
                "rejected": f"rejected by {latest.get('decided_by') or 'the reviewer'}",
            }.get(latest["status"], latest["status"])
            return _text(
                f"Your claim for {int(latest['claim_amount'])} rupees on {latest['policy_number']} is {state}. "
                "Anything else?"
            )
        if "hospitals" in result:
            if result["hospitals"]:
                return _text(
                    f"In {result['city']} your cashless network includes {', '.join(result['hospitals'])}. "
                    "Anything else?"
                )
            return _text(f"I don't see network hospitals listed for {result['city']}. Anything else?")
        if "premium_due" in result:
            if result["status"] == "paid":
                return _text(f"Good news - your premium is fully paid. The next due date is {result['next_due_date']}.")
            return _text(
                f"You have {result['premium_due']} rupees {result['status']}, due on {result['next_due_date']}."
            )
        if "coverage_type" in result:
            coverage = "both cashless and reimbursement" if result["coverage_type"] == "cashless_and_reimbursement" else "reimbursement only"
            active = "active" if result.get("active") else "lapsed - please renew it"
            return _text(
                f"Your {result['plan']} plan covers {coverage}, sum insured {result['sum_insured']} rupees. "
                f"The policy is {active}."
            )
        if "probability" in result:
            percent = round(result["probability"] * 100)
            if result.get("missing_documents"):
                missing = ", ".join(d.replace("_", " ") for d in result["missing_documents"])
                return _text(
                    f"Right now your claim acceptance probability is about {percent} percent. "
                    f"Adding your {missing} would improve it. Say 'submit' when you're ready."
                )
            return _text(
                f"Your claim acceptance probability is about {percent} percent. Say 'submit' and I'll queue it "
                "for review."
            )
        if result.get("queued_for_review"):
            return _text("Thanks - your claim is queued for human review. I'll confirm as soon as it's decided.")
        if "found" in result and result.get("found") is False:
            return _text("I couldn't find that policy number. Could you double-check it? It looks like POL-1001.")
        return _text("Done. Anything else I can help you with?")
