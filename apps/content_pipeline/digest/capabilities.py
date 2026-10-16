"""Reference table of our own offerings, practice areas, and case studies.

Fed into the digest LLM prompts (company digests only) so a channel summary
can map a detected theme — a named vendor, technology, or infrastructure
initiative — to one of our own offerings, instead of stopping at a generic
"reach out" angle. This is what lets a highlight like "Northern Trust
engages Lukka for digital-asset reporting" become "pitch API integration
of Lukka onto our ALTRO platform" rather than just a linked headline.

PLACEHOLDER CONTENT. Every description below needs to be replaced with the
real offering/case-study detail before a digest generated with this context
is sent to an account. Nothing here should be treated as fact by the LLM
beyond "this offering exists and covers these domains" — keep descriptions
short and factual once filled in, not marketing copy, since the model will
quote them back nearly verbatim in a sales pitch.
"""
from typing import Any, Dict, List



OFFERINGS: List[Dict[str, Any]] = [
    {
        "name": "KYRO",
        "domains": ["Data Strategy", "AI Adoption at scale"],
        "description": "PLACEHOLDER — one or two sentences on what KYRO is "
        "and the specific problem it solves.",
    },
    {
        "name": "FURO",
        "domains": ["Agentic AI"],
        "description": "PLACEHOLDER — one or two sentences on what FURO is "
        "and the specific problem it solves.",
    },
    {
        "name": "ALTRO",
        "domains": ["Blockchain", "Digital assets"],
        "description": "PLACEHOLDER — one or two sentences on what ALTRO is "
        "and the specific problem it solves.",
    },
    {
        "name": "ITSM (L1/L2/L3)",
        "domains": ["Infrastructure"],
        "description": "PLACEHOLDER — tiered IT service management practice. "
        "Matches any theme about revamping, modernizing, or outsourcing "
        "infrastructure/operations.",
    },
]

CASE_STUDIES: List[Dict[str, Any]] = [
    {
        "name": "BNY ETF platform engagement",
        "domains": ["ETFs", "Asset servicing"],
        "description": "PLACEHOLDER — describe the actual BNY ETF work "
        "(scope, what was delivered) so the model can cite it as a synergy "
        "angle against other custodians/asset managers building or "
        "expanding ETF platforms.",
    },
]

FIRM_PROFILE = (
    "PLACEHOLDER — one paragraph on firm background: e.g. 13 years of "
    "delivery experience in Blockchain, AI Adoption at scale, Data "
    "Strategy, and Agentic AI, focused on asset servicing and ETF clients "
    "in financial services."
)


def context_block() -> str:
    """Render the reference table as prompt text for the LLM.

    Kept as plain text (not JSON) since it's read-only context the model
    should reference, not data it echoes back structurally.
    """
    lines = [FIRM_PROFILE, "", "Our offerings:"]
    lines += [
        f"- {o['name']} ({', '.join(o['domains'])}): {o['description']}"
        for o in OFFERINGS
    ]
    lines += ["", "Case studies / synergy angles:"]
    lines += [
        f"- {c['name']} ({', '.join(c['domains'])}): {c['description']}"
        for c in CASE_STUDIES
    ]
    return "\n".join(lines)
