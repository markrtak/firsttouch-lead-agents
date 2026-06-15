"""FirstTouch by Mark Takla - inbound-lead-to-first-touch agent team core logic.

Single source of truth shared by the notebook (sircles_lead_agents.ipynb) and the
Streamlit UI (app.py). Contains the schemas, mock tools + fixtures, deterministic ICP
scoring, the LLM agent factory, the deterministic Arbiter resolution rule, and an
event-emitting orchestrator so a UI can render the workflow live.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal, Optional

from pydantic import BaseModel, Field
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq

# A streamer takes (step_kind, agent_name) and returns an optional token sink callback.
TokenSink = Callable[[str], None]
Streamer = Callable[[str, str], Optional[TokenSink]]


# =============================================================================
# Schemas & shared state
# =============================================================================
Disposition = Literal["pursue", "nurture", "skip"]
Action = Literal["auto_send", "draft_for_review", "nurture_sequence", "archive", "human_review"]


class RawLead(BaseModel):
    lead_id: str
    name: str
    title: str
    email: str
    company: str
    company_domain: Optional[str] = None
    source: str
    note: Optional[str] = None


class ContactEnrichment(BaseModel):
    full_name: str
    title: str
    seniority: Literal["exec", "director", "manager", "ic", "unknown"]
    email: str
    email_verified: bool
    linkedin_url: Optional[str] = None
    location: str


class CompanyEnrichment(BaseModel):
    name: str
    domain: Optional[str] = None
    industry: str
    employee_count: Optional[int] = None
    estimated_revenue: Optional[str] = None
    hq_location: str
    funding_stage: Optional[str] = None
    signals: list[str] = Field(default_factory=list)
    is_marketing_agency: bool = False
    has_inhouse_marketing: Optional[bool] = None


class Enrichment(BaseModel):
    contact: ContactEnrichment
    company: CompanyEnrichment


class ICPScore(BaseModel):
    total: int
    breakdown: dict[str, int]
    band: Disposition
    disqualifiers: list[str] = Field(default_factory=list)
    data_completeness: float


class Position(BaseModel):
    """One agent's reasoned debate stance."""
    agent: str = ""
    recommendation: Disposition
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    key_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Resolution(BaseModel):
    disposition: Disposition
    action: Action
    confidence: float
    method: str
    rationale: str
    winning_agent: Optional[str] = None
    dissent: Optional[dict] = None  # the losing position + why it lost
    escalate: bool = False
    escalation_reason: Optional[str] = None


class OutreachArtifacts(BaseModel):
    email_subject: str
    email_body: str
    linkedin_note: str


class CRMRecord(BaseModel):
    lead_id: str
    contact_name: str
    company: str
    deal_stage: str
    disposition: Disposition
    icp_score: int
    owner: str
    flags: list[str] = Field(default_factory=list)
    next_step: str
    notes: str


class TraceEvent(BaseModel):
    step: str
    agent: str
    summary: str
    data: dict = Field(default_factory=dict)
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class RunState(BaseModel):
    """Shared blackboard the Orchestrator threads through every step."""
    raw_lead: RawLead
    enrichment: Optional[Enrichment] = None
    icp: Optional[ICPScore] = None
    positions: list[Position] = Field(default_factory=list)
    rebuttals: list[Position] = Field(default_factory=list)
    resolution: Optional[Resolution] = None
    outreach: Optional[OutreachArtifacts] = None
    crm_record: Optional[CRMRecord] = None
    trace: list[TraceEvent] = Field(default_factory=list)

    def log(self, step: str, agent: str, summary: str, data: dict | None = None) -> None:
        self.trace.append(TraceEvent(step=step, agent=agent, summary=summary, data=data or {}))


# =============================================================================
# Mock tools & fixtures
# =============================================================================
_RAW_LEADS: list[dict] = [
    {
        "lead_id": "L-001", "name": "Mariam Hassan", "title": "VP of Marketing",
        "email": "mariam.hassan@nilebooks.io", "company": "NileBooks",
        "company_domain": "nilebooks.io", "source": "Website demo request",
        "note": "Filled in the 'book a demo' form, mentioned scaling paid acquisition.",
    },
    {
        "lead_id": "L-002", "name": "Tarek M.", "title": "Freelance Consultant",
        "email": "tarek.freelance92@gmail.com", "company": "Self-employed",
        "company_domain": None, "source": "Newsletter signup",
        "note": "Downloaded a free template.",
    },
    {
        "lead_id": "L-003", "name": "Omar Farouk", "title": "Head of Growth",
        "email": "omar@brightwave.agency", "company": "BrightWave Digital",
        "company_domain": "brightwave.agency", "source": "Contact form",
        "note": "Asked about our process and pricing in detail.",
    },
    {
        "lead_id": "L-004", "name": "Yasmine Adel", "title": "Founder",
        "email": "yasmine@zeitoun.app", "company": "Zeitoun",
        "company_domain": "zeitoun.app", "source": "Webinar attendee",
        "note": "Pre-seed founder, exploring options for later.",
    },
]

_CONTACT_FIXTURES: dict[str, dict] = {
    "L-001": {"full_name": "Mariam Hassan", "title": "VP of Marketing", "seniority": "exec",
              "email": "mariam.hassan@nilebooks.io", "email_verified": True,
              "linkedin_url": "https://linkedin.com/in/mariamhassan", "location": "Cairo, Egypt"},
    "L-002": {"full_name": "Tarek M.", "title": "Freelance Consultant", "seniority": "ic",
              "email": "tarek.freelance92@gmail.com", "email_verified": False,
              "linkedin_url": None, "location": "Unknown"},
    "L-003": {"full_name": "Omar Farouk", "title": "Head of Growth", "seniority": "director",
              "email": "omar@brightwave.agency", "email_verified": True,
              "linkedin_url": "https://linkedin.com/in/omarfarouk", "location": "Cairo, Egypt"},
    "L-004": {"full_name": "Yasmine Adel", "title": "Founder", "seniority": "exec",
              "email": "yasmine@zeitoun.app", "email_verified": True,
              "linkedin_url": "https://linkedin.com/in/yasmineadel", "location": "Alexandria, Egypt"},
}

_COMPANY_FIXTURES: dict[str, dict] = {
    "L-001": {"name": "NileBooks", "domain": "nilebooks.io", "industry": "B2B SaaS (fintech)",
              "employee_count": 120, "estimated_revenue": "$10M-$25M", "hq_location": "Cairo, Egypt",
              "funding_stage": "Series B",
              "signals": ["Hiring a growth marketer", "Recent Series B", "Website traffic up 40% QoQ"],
              "is_marketing_agency": False, "has_inhouse_marketing": False},
    "L-002": {"name": "Self-employed", "domain": None, "industry": "Unknown",
              "employee_count": 1, "estimated_revenue": "<$100K", "hq_location": "Unknown",
              "funding_stage": None, "signals": [],
              "is_marketing_agency": False, "has_inhouse_marketing": None},
    "L-003": {"name": "BrightWave Digital", "domain": "brightwave.agency",
              "industry": "Marketing & Advertising Agency",
              "employee_count": 45, "estimated_revenue": "$5M-$10M", "hq_location": "Cairo, Egypt",
              "funding_stage": "Bootstrapped",
              "signals": ["Active on LinkedIn", "Publishes client case studies"],
              "is_marketing_agency": True, "has_inhouse_marketing": True},
    "L-004": {"name": "Zeitoun", "domain": "zeitoun.app", "industry": "Consumer mobile app",
              "employee_count": 4, "estimated_revenue": "<$250K", "hq_location": "Alexandria, Egypt",
              "funding_stage": "Pre-seed",
              "signals": ["Early traction", "No marketing budget yet"],
              "is_marketing_agency": False, "has_inhouse_marketing": False},
}

# In-memory mock CRM (HubSpot stand-in)
_CRM_STORE: dict[str, dict] = {}


def fetch_new_leads() -> list[RawLead]:
    """Mock lead scanner. Returns raw inbound leads."""
    return [RawLead(**copy.deepcopy(r)) for r in _RAW_LEADS]


def enrich_contact(lead: RawLead) -> ContactEnrichment:
    """Mock Apollo/Hunter contact enrichment."""
    data = _CONTACT_FIXTURES.get(lead.lead_id)
    if data is None:
        data = {"full_name": lead.name, "title": lead.title, "seniority": "unknown",
                "email": lead.email, "email_verified": False, "linkedin_url": None,
                "location": "Unknown"}
    return ContactEnrichment(**copy.deepcopy(data))


def enrich_company(lead: RawLead) -> CompanyEnrichment:
    """Mock Clay/Apollo company enrichment."""
    data = _COMPANY_FIXTURES.get(lead.lead_id)
    if data is None:
        data = {"name": lead.company, "domain": lead.company_domain, "industry": "Unknown",
                "employee_count": None, "estimated_revenue": None, "hq_location": "Unknown",
                "funding_stage": None, "signals": [], "is_marketing_agency": False,
                "has_inhouse_marketing": None}
    return CompanyEnrichment(**copy.deepcopy(data))


def upsert_crm_record(record: CRMRecord) -> str:
    """Mock HubSpot upsert. Returns the CRM id."""
    _CRM_STORE[record.lead_id] = record.model_dump()
    return record.lead_id


# =============================================================================
# ICP scoring (deterministic)
# =============================================================================
TARGET_INDUSTRIES = {"b2b saas", "saas", "fintech", "e-commerce", "ecommerce",
                     "consumer mobile app", "consumer app", "professional services"}
ADJACENT_INDUSTRIES = {"healthcare", "education", "real estate", "hospitality"}
EGYPT_TERMS = {"egypt", "cairo", "alexandria", "giza"}
MENA_TERMS = {"uae", "dubai", "abu dhabi", "saudi", "riyadh", "jordan", "amman",
              "qatar", "doha", "kuwait", "bahrain", "morocco", "tunisia", "lebanon"}
FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}
PURSUE_BAND, NURTURE_BAND = 70, 40


def _score_seniority(seniority: str) -> int:
    return {"exec": 20, "director": 15, "manager": 8, "ic": 3, "unknown": 0}.get(seniority, 0)


def _score_size(employees: Optional[int]) -> int:
    if employees is None:
        return 0
    if 20 <= employees <= 500:
        return 20
    if 10 <= employees < 20 or 500 < employees <= 2000:
        return 12
    if employees > 2000:
        return 8
    return 4


def _score_industry(industry: str) -> int:
    i = (industry or "").lower()
    if any(t in i for t in TARGET_INDUSTRIES):
        return 20
    if any(t in i for t in ADJACENT_INDUSTRIES):
        return 10
    return 0


def _score_location(*locations: str) -> int:
    blob = " ".join(l.lower() for l in locations if l)
    if any(t in blob for t in EGYPT_TERMS):
        return 15
    if any(t in blob for t in MENA_TERMS):
        return 8
    if blob and "unknown" not in blob:
        return 3
    return 0


def _score_signals(signals: list[str]) -> int:
    positive = [s for s in signals if "no marketing budget" not in s.lower()]
    return min(15, 5 * len(positive))


def _score_maturity(has_inhouse: Optional[bool]) -> int:
    if has_inhouse is False:
        return 10
    if has_inhouse is True:
        return 3
    return 6


def _detect_disqualifiers(e: Enrichment) -> list[str]:
    flags: list[str] = []
    if e.company.is_marketing_agency:
        flags.append("competing_agency")
    title = e.contact.title.lower()
    if any(t in title for t in ("student", "intern", "seeking", "job seeker", "looking for")):
        flags.append("student_jobseeker")
    domain = (e.contact.email.split("@")[-1] if "@" in e.contact.email else "").lower()
    tiny = (e.company.employee_count or 0) <= 2
    no_signals = len(e.company.signals) == 0
    if domain in FREE_EMAIL_DOMAINS and tiny and no_signals:
        flags.append("no_budget_micro")
    return flags


def _data_completeness(e: Enrichment) -> float:
    checks = [
        e.contact.email_verified,
        bool(e.contact.linkedin_url),
        e.contact.seniority != "unknown",
        e.contact.location.lower() != "unknown",
        e.company.employee_count is not None,
        e.company.industry.lower() != "unknown",
        e.company.funding_stage is not None,
        e.company.has_inhouse_marketing is not None,
    ]
    return round(sum(1 for c in checks if c) / len(checks), 2)


def score_icp(e: Enrichment) -> ICPScore:
    breakdown = {
        "seniority": _score_seniority(e.contact.seniority),
        "size_budget": _score_size(e.company.employee_count),
        "industry": _score_industry(e.company.industry),
        "location": _score_location(e.contact.location, e.company.hq_location),
        "signals": _score_signals(e.company.signals),
        "maturity_gap": _score_maturity(e.company.has_inhouse_marketing),
    }
    total = sum(breakdown.values())
    band: Disposition = "pursue" if total >= PURSUE_BAND else "nurture" if total >= NURTURE_BAND else "skip"

    # Budget-readiness rule: an explicit "no budget (yet)" signal caps a strong-fit lead at
    # nurture - they are a great future client, just not a retainer we can close today.
    explicit_no_budget = any("no budget" in s.lower() or "no marketing budget" in s.lower()
                             for s in e.company.signals)
    if band == "pursue" and explicit_no_budget:
        band = "nurture"

    return ICPScore(
        total=total, breakdown=breakdown, band=band,
        disqualifiers=_detect_disqualifiers(e), data_completeness=_data_completeness(e),
    )


# =============================================================================
# LLM + agent factory
# =============================================================================
def build_llm(model: str = "llama-3.3-70b-versatile", temperature: float = 0.2) -> ChatGroq:
    """Build the shared Groq LLM client. Reads GROQ_API_KEY from the environment."""
    return ChatGroq(model=model, temperature=temperature)


class _TokenStreamHandler(BaseCallbackHandler):
    """Routes streamed LLM tokens to a UI-provided sink callback."""

    def __init__(self, sink: TokenSink) -> None:
        self._sink = sink

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if token:
            self._sink(token)


def lead_brief(state: RunState) -> str:
    """Compact, factual brief the debate agents reason over."""
    e, icp = state.enrichment, state.icp
    return json.dumps({
        "contact": e.contact.model_dump(),
        "company": e.company.model_dump(),
        "icp_score_total": icp.total,
        "icp_breakdown": icp.breakdown,
        "icp_band": icp.band,
        "disqualifiers": icp.disqualifiers,
        "data_completeness": icp.data_completeness,
        "inbound_note": state.raw_lead.note,
    }, indent=2)


_INTEL_SYS = (
    "You are the Lead Intelligence analyst at Sircles, a marketing agency based in Egypt. "
    "Summarise this enriched lead in 3-4 crisp sentences a sales team can act on: who they are, "
    "fit highlights, and the single biggest risk. Be factual and neutral - do not recommend an action."
)
_ADVOCATE_SYS = (
    "You are the GROWTH ADVOCATE in a sales debate at Sircles (a marketing agency based in Egypt). "
    "Your lens is opportunity and revenue: a single qualified lead can become a multi-month retainer. "
    "Make the strongest HONEST case to PURSUE (or at least NURTURE). You are not a yes-man - if the lead "
    "is genuinely hopeless you may concede 'skip' - but your default bias is to find the upside. "
    "Ground every claim in the brief; cite concrete evidence. Also list the risks you are choosing to "
    "discount, so the debate is honest. Confidence (0-1) = how strong you believe the opportunity is."
)
_SKEPTIC_SYS = (
    "You are the RISK SKEPTIC (devil's advocate) in a sales debate at Sircles (a marketing agency in Egypt). "
    "Your lens is the cost of being wrong: chasing bad leads drains the team, and a clumsy first-touch to the "
    "wrong prospect can burn them permanently. Hunt for red flags: weak fit, competitor/agency, no budget, "
    "missing decision-maker, thin or unverified data, wrong geography. Argue to SKIP or NURTURE unless the "
    "case is airtight. Ground every claim in the brief. Confidence (0-1) = how sure you are of the risk."
)
_DEBATE_HUMAN = (
    "Intelligence brief:\n{intel}\n\nStructured lead data:\n{brief}\n\n"
    "Give your position: recommendation (pursue/nurture/skip), confidence 0-1, rationale, "
    "key_evidence (bullet facts), and risks."
)
_REBUTTAL_HUMAN = (
    "Intelligence brief:\n{intel}\n\nStructured lead data:\n{brief}\n\n"
    "The OPPOSING agent argued:\n{opponent}\n\n"
    "Respond to their strongest point. You may keep or revise your recommendation and confidence, "
    "but justify any change. Return your updated position."
)
_OUTREACH_SYS = (
    "You are an SDR at Sircles, a marketing agency based in Egypt. Draft a personalised, concise "
    "first-touch EMAIL and a short LinkedIn connection note for this prospect. Ground everything in the "
    "enriched facts and the inbound note - no invented specifics. Warm, professional, not gimmicky. "
    "Email body under 130 words with one clear call to action."
)
_CRM_NOTES_SYS = (
    "You are a CRM Hygiene agent. In 2-3 sentences, write a clean internal note for the CRM summarising "
    "the decision and why, including the losing argument if the team debated. Factual, no fluff."
)
_DEAL_STAGE = {
    "auto_send": "Outreach Sent",
    "draft_for_review": "Outreach Drafted - Needs Review",
    "nurture_sequence": "Nurture",
    "archive": "Disqualified",
    "human_review": "Needs Human Review",
}
_NEXT_STEP = {
    "auto_send": "First-touch sent; monitor for reply.",
    "draft_for_review": "SDR to review draft before sending.",
    "nurture_sequence": "Enroll in nurture sequence; revisit next quarter.",
    "archive": "Archived as not a fit.",
    "human_review": "Route to a human for a judgement call.",
}


class Agents:
    """LLM-backed agent team built around a single injected LLM."""

    def __init__(self, llm: ChatGroq) -> None:
        self.llm = llm
        self._intel = ChatPromptTemplate.from_messages(
            [("system", _INTEL_SYS), ("human", "Enriched lead:\n{brief}")]) | llm
        self._advocate = (ChatPromptTemplate.from_messages([("system", _ADVOCATE_SYS), ("human", _DEBATE_HUMAN)])
                          | llm.with_structured_output(Position))
        self._skeptic = (ChatPromptTemplate.from_messages([("system", _SKEPTIC_SYS), ("human", _DEBATE_HUMAN)])
                         | llm.with_structured_output(Position))
        self._advocate_reb = (ChatPromptTemplate.from_messages([("system", _ADVOCATE_SYS), ("human", _REBUTTAL_HUMAN)])
                              | llm.with_structured_output(Position))
        self._skeptic_reb = (ChatPromptTemplate.from_messages([("system", _SKEPTIC_SYS), ("human", _REBUTTAL_HUMAN)])
                             | llm.with_structured_output(Position))
        self._outreach = (ChatPromptTemplate.from_messages([("system", _OUTREACH_SYS),
                          ("human", "Enriched lead and fit:\n{brief}\n\nIntelligence brief:\n{intel}")])
                          | llm.with_structured_output(OutreachArtifacts))
        self._crm_notes = ChatPromptTemplate.from_messages(
            [("system", _CRM_NOTES_SYS), ("human", "Decision:\n{resolution}\n\nLead brief:\n{brief}")]) | llm

    @staticmethod
    def _cfg(on_token: Optional[TokenSink]) -> dict:
        return {"callbacks": [_TokenStreamHandler(on_token)]} if on_token else {}

    def lead_intelligence(self, state: RunState, on_token: Optional[TokenSink] = None) -> str:
        if on_token:
            chunks: list[str] = []
            for ch in self._intel.stream({"brief": lead_brief(state)}):
                token = ch.content or ""
                if token:
                    chunks.append(token)
                    on_token(token)
            return "".join(chunks)
        return self._intel.invoke({"brief": lead_brief(state)}).content

    def advocate(self, state: RunState, intel: str, on_token: Optional[TokenSink] = None) -> Position:
        p = self._advocate.invoke({"intel": intel, "brief": lead_brief(state)}, config=self._cfg(on_token))
        p.agent = "Growth Advocate"
        return p

    def skeptic(self, state: RunState, intel: str, on_token: Optional[TokenSink] = None) -> Position:
        p = self._skeptic.invoke({"intel": intel, "brief": lead_brief(state)}, config=self._cfg(on_token))
        p.agent = "Risk Skeptic"
        return p

    def rebuttal(self, agent_name: str, state: RunState, intel: str, opponent: Position,
                 on_token: Optional[TokenSink] = None) -> Position:
        runnable = self._advocate_reb if agent_name == "Growth Advocate" else self._skeptic_reb
        p = runnable.invoke(
            {"intel": intel, "brief": lead_brief(state), "opponent": opponent.model_dump_json(indent=2)},
            config=self._cfg(on_token))
        p.agent = agent_name
        return p

    def outreach(self, state: RunState, intel: str, on_token: Optional[TokenSink] = None) -> OutreachArtifacts:
        return self._outreach.invoke({"brief": lead_brief(state), "intel": intel}, config=self._cfg(on_token))

    def crm_hygiene(self, state: RunState) -> CRMRecord:
        res, e, icp = state.resolution, state.enrichment, state.icp
        flags = list(icp.disqualifiers)
        if res.escalate:
            flags.append(f"escalate:{res.escalation_reason}")
        owner = "Human (SDR lead)" if res.escalate or res.action == "draft_for_review" else "Auto-pipeline"
        notes = self._crm_notes.invoke(
            {"resolution": res.model_dump_json(indent=2), "brief": lead_brief(state)}).content
        return CRMRecord(
            lead_id=state.raw_lead.lead_id, contact_name=e.contact.full_name, company=e.company.name,
            deal_stage=_DEAL_STAGE[res.action], disposition=res.disposition, icp_score=icp.total,
            owner=owner, flags=flags, next_step=_NEXT_STEP[res.action], notes=notes,
        )


def build_agents(llm: ChatGroq) -> Agents:
    return Agents(llm)


# =============================================================================
# Debate resolution (deterministic Arbiter)
# =============================================================================
MARGIN = 0.15            # confidence gap below which a disagreement is "contested"
CONF_THRESHOLD = 0.70    # min confidence to act autonomously on a pursue
MIN_DATA = 0.50          # min data completeness to act at all


def _gate_action(disposition: Disposition, confidence: float, escalate: bool) -> Action:
    if escalate:
        return "human_review"
    if disposition == "pursue":
        return "auto_send" if confidence >= CONF_THRESHOLD else "draft_for_review"
    if disposition == "nurture":
        return "nurture_sequence"
    return "archive"


def _dissent(pos: Position, why: str) -> dict:
    return {"agent": pos.agent, "recommendation": pos.recommendation, "confidence": pos.confidence,
            "rationale": pos.rationale, "why_it_lost": why}


def is_contested(advocate: Position, skeptic: Position) -> bool:
    """True when the agents disagree with comparable confidence -> needs a rebuttal."""
    return (advocate.recommendation != skeptic.recommendation
            and abs(advocate.confidence - skeptic.confidence) < MARGIN)


def resolve(icp: ICPScore, advocate: Position, skeptic: Position,
            rebuttal_done: bool = False) -> Resolution:
    # 1. Critic veto: competing agency -> human call (conflict vs partnership)
    if "competing_agency" in icp.disqualifiers:
        return Resolution(
            disposition="skip", action="human_review", confidence=0.9,
            method="critic_veto:competing_agency",
            rationale=("Lead is itself a marketing agency. Could be a competitor or a partnership/"
                       "white-label opportunity - not an autopilot decision. Escalating to a human."),
            winning_agent="Risk Skeptic",
            dissent=_dissent(advocate, "Veto on competitor conflict overrides the growth case."),
            escalate=True, escalation_reason="Competing agency - potential conflict or partnership",
        )

    # 2. Hard disqualifiers -> skip
    hard = [d for d in icp.disqualifiers if d in ("student_jobseeker", "no_budget_micro")]
    if hard:
        return Resolution(
            disposition="skip", action="archive", confidence=0.9,
            method=f"hard_disqualifier:{','.join(hard)}",
            rationale=f"Disqualifier(s) {hard} make this not a fit for a retainer.",
            winning_agent="Risk Skeptic",
            dissent=(_dissent(advocate, "Disqualifier overrides the growth case.")
                     if advocate.recommendation != "skip" else None),
            escalate=False,
        )

    # 3. Thin data -> don't act
    if icp.data_completeness < MIN_DATA:
        return Resolution(
            disposition=icp.band, action="human_review", confidence=0.4,
            method="low_data_confidence",
            rationale=(f"Data completeness {icp.data_completeness} is below {MIN_DATA}; "
                       "not enough to act autonomously."),
            winning_agent=None,
            dissent=_dissent(advocate if advocate.recommendation != skeptic.recommendation else skeptic,
                             "Insufficient data to adjudicate the disagreement."),
            escalate=True, escalation_reason="Insufficient enrichment data",
        )

    # 4. Consensus
    if advocate.recommendation == skeptic.recommendation:
        disp = advocate.recommendation
        conf = round(min(0.95, (advocate.confidence + skeptic.confidence) / 2 + 0.10), 2)
        return Resolution(
            disposition=disp, action=_gate_action(disp, conf, False), confidence=conf,
            method="consensus",
            rationale=f"Both agents independently recommend '{disp}'.",
            winning_agent="Both (consensus)", dissent=None, escalate=False,
        )

    # 5/6. Disagreement between the two agents.
    gap = abs(advocate.confidence - skeptic.confidence)
    recs = {advocate.recommendation, skeptic.recommendation}
    polar = recs == {"pursue", "skip"}  # the extremes - a genuine high-stakes split

    # Decisive confidence difference -> confidence-weighted vote (applies either round).
    if gap >= MARGIN:
        winner, loser = ((advocate, skeptic) if advocate.confidence > skeptic.confidence
                         else (skeptic, advocate))
        disp = winner.recommendation
        conf = round(winner.confidence, 2)
        return Resolution(
            disposition=disp, action=_gate_action(disp, conf, False), confidence=conf,
            method="confidence_weighted_vote",
            rationale=(f"{winner.agent} prevailed ({winner.confidence:.2f} vs {loser.confidence:.2f} "
                       f"confidence) recommending '{disp}'."),
            winning_agent=winner.agent,
            dissent=_dissent(loser, f"Lower confidence ({loser.confidence:.2f}) than the winner."),
            escalate=False,
        )

    # Contested (comparable confidence). Give the team one bounded rebuttal first.
    if not rebuttal_done:
        return Resolution(
            disposition=icp.band, action="human_review", confidence=round(gap, 2),
            method="contested:needs_rebuttal",
            rationale="Agents disagree with comparable confidence; triggering a rebuttal round.",
            winning_agent=None, dissent=None, escalate=False,
        )

    # Still contested after the rebuttal.
    if polar:
        avg = round((advocate.confidence + skeptic.confidence) / 2, 2)
        return Resolution(
            disposition=icp.band, action="human_review", confidence=avg,
            method="contested_escalation",
            rationale=("Even after a rebuttal the team is split between pursue and skip with comparable "
                       "confidence. Escalating rather than forcing a high-stakes decision."),
            winning_agent=None,
            dissent={"advocate": _dissent(advocate, "Unresolved"),
                     "skeptic": _dissent(skeptic, "Unresolved")},
            escalate=True, escalation_reason="Debate did not converge on a high-stakes split",
        )

    # Adjacent split (pursue/nurture or nurture/skip): the ICP band breaks the tie - no human needed.
    if icp.band in recs:
        matcher = advocate if advocate.recommendation == icp.band else skeptic
        other = skeptic if matcher is advocate else advocate
        disp = icp.band
        conf = round(matcher.confidence, 2)
        return Resolution(
            disposition=disp, action=_gate_action(disp, conf, False), confidence=conf,
            method="icp_prior_tiebreak",
            rationale=(f"Adjacent split {sorted(recs)} with close confidence; the ICP band '{icp.band}' "
                       f"(score {icp.total}) breaks the tie for {matcher.agent}."),
            winning_agent=matcher.agent,
            dissent=_dissent(other, f"ICP band '{icp.band}' favoured the opposing call."),
            escalate=False,
        )

    # Adjacent split that is off the ICP band: fall back to the more confident agent.
    winner, loser = ((advocate, skeptic) if advocate.confidence >= skeptic.confidence
                     else (skeptic, advocate))
    disp = winner.recommendation
    conf = round(winner.confidence, 2)
    return Resolution(
        disposition=disp, action=_gate_action(disp, conf, False), confidence=conf,
        method="confidence_weighted_vote",
        rationale=f"Adjacent split off the ICP band; {winner.agent}'s call taken on confidence.",
        winning_agent=winner.agent,
        dissent=_dissent(loser, "Lower/equal confidence and off the ICP band."),
        escalate=False,
    )


# =============================================================================
# Orchestrator (event-emitting) + trace
# =============================================================================
@dataclass
class Event:
    """A single step in the run, emitted live by the orchestrator."""
    kind: str                      # intake | enriched | brief | position | rebuttal | resolution | outreach | crm | done
    state: RunState
    payload: Any = None
    note: str = ""


def run_pipeline_events(lead: RawLead, agents: Agents, streamer: Optional[Streamer] = None) -> Iterator[Event]:
    """Run the workflow, yielding an Event after each step so a UI can render it live.

    `streamer(kind, agent)` may return a token sink to receive streamed LLM tokens for that step.
    """
    def sink(kind: str, agent: str) -> Optional[TokenSink]:
        return streamer(kind, agent) if streamer else None

    state = RunState(raw_lead=lead)
    state.log("intake", "Orchestrator", f"Received lead {lead.lead_id} ({lead.name}, {lead.company}).")
    yield Event("intake", state)

    # Enrich + score
    state.enrichment = Enrichment(contact=enrich_contact(lead), company=enrich_company(lead))
    state.icp = score_icp(state.enrichment)
    state.log("enrich", "Lead Intelligence",
              f"Enriched + scored. ICP={state.icp.total} band={state.icp.band} "
              f"disqualifiers={state.icp.disqualifiers or 'none'}.",
              {"breakdown": state.icp.breakdown, "data_completeness": state.icp.data_completeness})
    yield Event("enriched", state)

    intel = agents.lead_intelligence(state, on_token=sink("brief", "Lead Intelligence"))
    state.log("brief", "Lead Intelligence", intel)
    yield Event("brief", state, payload=intel)

    # Debate round 1
    advocate = agents.advocate(state, intel, on_token=sink("advocate", "Growth Advocate"))
    state.positions = [advocate]
    state.log("debate", "Growth Advocate",
              f"{advocate.recommendation} @ conf {advocate.confidence:.2f}: {advocate.rationale}",
              advocate.model_dump())
    yield Event("position", state, payload=advocate)

    skeptic = agents.skeptic(state, intel, on_token=sink("skeptic", "Risk Skeptic"))
    state.positions = [advocate, skeptic]
    state.log("debate", "Risk Skeptic",
              f"{skeptic.recommendation} @ conf {skeptic.confidence:.2f}: {skeptic.rationale}",
              skeptic.model_dump())
    yield Event("position", state, payload=skeptic)

    # Resolve, with at most one bounded rebuttal
    resolution = resolve(state.icp, advocate, skeptic, rebuttal_done=False)
    if resolution.method == "contested:needs_rebuttal":
        state.log("debate", "Arbiter", "Contested: confidences too close. Triggering one rebuttal round.")
        yield Event("rebuttal_start", state, note="Contested - one bounded rebuttal round.")
        advocate2 = agents.rebuttal("Growth Advocate", state, intel, skeptic,
                                    on_token=sink("rebuttal_advocate", "Growth Advocate"))
        skeptic2 = agents.rebuttal("Risk Skeptic", state, intel, advocate,
                                   on_token=sink("rebuttal_skeptic", "Risk Skeptic"))
        state.rebuttals = [advocate2, skeptic2]
        state.log("rebuttal", "Growth Advocate",
                  f"{advocate2.recommendation} @ conf {advocate2.confidence:.2f}: {advocate2.rationale}",
                  advocate2.model_dump())
        state.log("rebuttal", "Risk Skeptic",
                  f"{skeptic2.recommendation} @ conf {skeptic2.confidence:.2f}: {skeptic2.rationale}",
                  skeptic2.model_dump())
        yield Event("rebuttal", state, payload=(advocate2, skeptic2))
        resolution = resolve(state.icp, advocate2, skeptic2, rebuttal_done=True)

    state.resolution = resolution
    state.log("resolve", "Arbiter",
              f"{resolution.method} -> {resolution.disposition} / {resolution.action} "
              f"(conf {resolution.confidence}). {resolution.rationale}",
              resolution.model_dump())
    yield Event("resolution", state, payload=resolution)

    # Handoffs
    if resolution.disposition == "pursue" and not resolution.escalate:
        state.outreach = agents.outreach(state, intel, on_token=sink("outreach", "SDR / Outreach"))
        state.log("outreach", "SDR / Outreach",
                  f"Drafted email + LinkedIn note (action={resolution.action}).",
                  state.outreach.model_dump())
        yield Event("outreach", state, payload=state.outreach)

    state.crm_record = agents.crm_hygiene(state)
    crm_id = upsert_crm_record(state.crm_record)
    state.log("crm", "CRM Hygiene",
              f"Upserted CRM record {crm_id}: stage='{state.crm_record.deal_stage}', "
              f"owner='{state.crm_record.owner}', flags={state.crm_record.flags or 'none'}.",
              state.crm_record.model_dump())
    yield Event("crm", state, payload=state.crm_record)

    yield Event("done", state)


def run_pipeline(lead: RawLead, agents: Agents) -> RunState:
    """Drain the event stream and return the final RunState (used by the notebook/tests)."""
    state: Optional[RunState] = None
    for event in run_pipeline_events(lead, agents):
        state = event.state
    return state


def _md_positions(positions: list[Position]) -> str:
    rows = []
    for p in positions:
        ev = "; ".join(p.key_evidence) or "-"
        rk = "; ".join(p.risks) or "-"
        rows.append(f"- **{p.agent}** -> `{p.recommendation}` @ confidence **{p.confidence:.2f}**\n"
                    f"  - Rationale: {p.rationale}\n  - Evidence: {ev}\n  - Risks: {rk}")
    return "\n".join(rows)


def render_trace(state: RunState) -> str:
    """Readable, non-author-friendly account of the whole run."""
    s, icp, res = state, state.icp, state.resolution
    lines: list[str] = []
    lines.append(f"# Run trace - {s.raw_lead.lead_id}: {s.raw_lead.name} @ {s.raw_lead.company}")
    lines.append(f"_Source: {s.raw_lead.source}. Inbound note: {s.raw_lead.note or '-'}_\n")

    lines.append("## 1. Enrichment & ICP score")
    lines.append(f"- Contact: {s.enrichment.contact.title}, seniority `{s.enrichment.contact.seniority}`, "
                 f"{s.enrichment.contact.location}, email verified: {s.enrichment.contact.email_verified}")
    lines.append(f"- Company: {s.enrichment.company.industry}, "
                 f"{s.enrichment.company.employee_count} employees, "
                 f"{s.enrichment.company.funding_stage}, agency: {s.enrichment.company.is_marketing_agency}")
    lines.append(f"- Signals: {', '.join(s.enrichment.company.signals) or 'none'}")
    lines.append(f"- **ICP score: {icp.total}/100** (band `{icp.band}`) | breakdown: {icp.breakdown}")
    lines.append(f"- Disqualifiers: {icp.disqualifiers or 'none'} | data completeness: {icp.data_completeness}\n")

    lines.append("## 2. Debate (round 1)")
    lines.append(_md_positions(s.positions))
    if s.rebuttals:
        lines.append("\n## 2b. Rebuttal round")
        lines.append(_md_positions(s.rebuttals))

    lines.append("\n## 3. Resolution")
    lines.append(f"- Method: `{res.method}`")
    lines.append(f"- **Disposition: `{res.disposition}` -> action: `{res.action}`** (confidence {res.confidence})")
    lines.append(f"- Winning position: {res.winning_agent or 'n/a'}")
    lines.append(f"- Rationale: {res.rationale}")
    if res.dissent:
        lines.append(f"- **Recorded dissent:** {json.dumps(res.dissent, indent=2)}")
    if res.escalate:
        lines.append(f"- **ESCALATED TO HUMAN:** {res.escalation_reason}")

    if s.outreach:
        lines.append("\n## 4. Outreach artifacts")
        lines.append(f"**Subject:** {s.outreach.email_subject}\n")
        lines.append(f"**Email:**\n\n{s.outreach.email_body}\n")
        lines.append(f"**LinkedIn note:** {s.outreach.linkedin_note}")
    else:
        lines.append("\n## 4. Outreach artifacts\n_None - no autonomous first-touch for this disposition._")

    lines.append("\n## 5. CRM record (mock HubSpot)")
    lines.append(f"```json\n{json.dumps(s.crm_record.model_dump(), indent=2)}\n```")
    return "\n".join(lines)
