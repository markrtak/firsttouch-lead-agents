"""Sircles - real-time lead debate UI.

Watch the agent team work a lead live: enrichment + ICP score, the Advocate vs Skeptic
debate, the Arbiter's resolution rule, and the outreach/CRM handoffs. Brand-themed to match
Sircles (black background, magenta accent).

Run:  streamlit run app.py
"""
import html
import json
import os

import streamlit as st
from dotenv import load_dotenv

import sircles_core as core

load_dotenv()

st.set_page_config(page_title="FirstTouch by Mark Takla", layout="wide")

MAGENTA = "#EC008C"

CSS = """
<style>
  .block-container { padding-top: 3.2rem; }
  .sircles-title { font-size: 1.6rem; font-weight: 800; letter-spacing: .5px;
                   line-height: 1.35; padding: 2px 0; white-space: nowrap; }
  .sircles-title span { color: #EC008C; }
  .sec-h { color: #EC008C; font-weight: 700; letter-spacing: .4px; text-transform: uppercase;
           font-size: .8rem; margin: 18px 0 6px; }
  .card { background: #17171B; border: 1px solid #2a2a30; border-radius: 12px;
          padding: 16px 18px; margin-bottom: 12px; }
  .card.adv  { border-left: 4px solid #EC008C; }
  .card.skep { border-left: 4px solid #9aa0a6; }
  .card.plain{ border-left: 4px solid #3a3a40; }
  .card h4 { margin: 0 0 8px; font-size: 1.02rem; }
  .pill { display: inline-block; padding: 2px 11px; border-radius: 999px; font-size: .72rem;
          font-weight: 700; text-transform: uppercase; letter-spacing: .4px; }
  .pill.pursue  { background: #EC008C; color: #fff; }
  .pill.nurture { background: #7a3cff; color: #fff; }
  .pill.skip    { background: #3a3a40; color: #e6e6e6; }
  .pill.escalate{ background: #ff3b30; color: #fff; }
  .confwrap { background: #2a2a30; border-radius: 6px; height: 9px; width: 100%;
              overflow: hidden; margin: 8px 0 10px; }
  .conffill { background: #EC008C; height: 9px; }
  .muted { color: #9aa0a6; font-size: .82rem; }
  .kv { font-size: .9rem; line-height: 1.5; }
  .kv b { color: #fff; }
  ul.tight { margin: 4px 0 0; padding-left: 18px; }
  ul.tight li { font-size: .86rem; margin-bottom: 2px; }
  .typing::after { content: " |"; color: #EC008C; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def esc(text: str) -> str:
    return html.escape(str(text or ""))


def _pill(label: str, cls: str) -> str:
    return f'<span class="pill {cls}">{esc(label)}</span>'


def _conf_bar(conf: float) -> str:
    pct = max(0, min(100, round(conf * 100)))
    return (f'<div class="muted">confidence {conf:.2f}</div>'
            f'<div class="confwrap"><div class="conffill" style="width:{pct}%"></div></div>')


def thinking_card(agent: str, accent: str) -> str:
    return (f'<div class="card {accent}"><h4>{esc(agent)}</h4>'
            f'<div class="muted typing">thinking</div></div>')


def stream_card(agent: str, accent: str, text: str) -> str:
    return (f'<div class="card {accent}"><h4>{esc(agent)}</h4>'
            f'<div class="kv typing">{esc(text)}</div></div>')


def position_card(p: core.Position, accent: str) -> str:
    ev = "".join(f"<li>{esc(x)}</li>" for x in p.key_evidence) or "<li>-</li>"
    rk = "".join(f"<li>{esc(x)}</li>" for x in p.risks) or "<li>-</li>"
    return (
        f'<div class="card {accent}">'
        f'<h4>{esc(p.agent)} &nbsp; {_pill(p.recommendation, p.recommendation)}</h4>'
        f'{_conf_bar(p.confidence)}'
        f'<div class="kv">{esc(p.rationale)}</div>'
        f'<div class="sec-h">Evidence</div><ul class="tight">{ev}</ul>'
        f'<div class="sec-h">Risks discounted/seen</div><ul class="tight">{rk}</ul>'
        f'</div>'
    )


def render_lead(box, lead: core.RawLead) -> None:
    box.markdown(
        f'<div class="card plain"><h4>{esc(lead.name)} &middot; {esc(lead.title)}</h4>'
        f'<div class="kv"><b>{esc(lead.company)}</b> &nbsp;|&nbsp; {esc(lead.email)}</div>'
        f'<div class="muted">Source: {esc(lead.source)} &nbsp;|&nbsp; "{esc(lead.note)}"</div></div>',
        unsafe_allow_html=True,
    )


def render_icp(box, icp: core.ICPScore) -> None:
    band_cls = icp.band
    bars = ""
    maxes = {"seniority": 20, "size_budget": 20, "industry": 20, "location": 15,
             "signals": 15, "maturity_gap": 10}
    for k, mx in maxes.items():
        v = icp.breakdown.get(k, 0)
        pct = round(v / mx * 100) if mx else 0
        bars += (f'<div class="kv">{esc(k)} &middot; {v}/{mx}</div>'
                 f'<div class="confwrap"><div class="conffill" style="width:{pct}%"></div></div>')
    disq = ", ".join(icp.disqualifiers) if icp.disqualifiers else "none"
    box.markdown(
        f'<div class="card plain"><h4>ICP score {icp.total}/100 &nbsp; {_pill(icp.band, band_cls)}</h4>'
        f'{bars}'
        f'<div class="muted">Disqualifiers: <b>{esc(disq)}</b> &nbsp;|&nbsp; '
        f'data completeness: {icp.data_completeness}</div></div>',
        unsafe_allow_html=True,
    )


def render_resolution(box, res: core.Resolution) -> None:
    with box:
        st.markdown('<div class="sec-h">Arbiter verdict</div>', unsafe_allow_html=True)
        disp_pill = _pill(res.disposition, "escalate" if res.escalate else res.disposition)
        st.markdown(
            f'<div class="card plain"><h4>{disp_pill} &rarr; '
            f'<code>{esc(res.action)}</code></h4>'
            f'<div class="muted">rule: <b>{esc(res.method)}</b> &nbsp;|&nbsp; '
            f'confidence {res.confidence}</div>'
            f'<div class="kv" style="margin-top:8px">{esc(res.rationale)}</div></div>',
            unsafe_allow_html=True,
        )
        if res.escalate:
            st.error(f"Escalated to a human: {res.escalation_reason}")
        if res.dissent:
            with st.expander("Recorded dissent (the losing argument)"):
                st.json(res.dissent)


def render_outreach(box, art: core.OutreachArtifacts) -> None:
    with box:
        st.markdown('<div class="sec-h">Outreach artifacts</div>', unsafe_allow_html=True)
        st.markdown(f"**Subject:** {art.email_subject}")
        st.markdown(f"**Email**\n\n{art.email_body}")
        st.markdown(f"**LinkedIn note:** {art.linkedin_note}")


@st.dialog("Enrichment record · stub for Apollo / Clay / Hunter", width="large")
def show_enrichment_record(lead: core.RawLead) -> None:
    st.caption(
        "Mock enrichment. In production these fields are fetched from external tools - "
        "**Apollo / Hunter** for the contact and **Clay / Apollo** for the company. Here they "
        "are stubbed from in-code fixtures in `sircles_core.py`, so the values are fixed per lead."
    )
    contact = core.enrich_contact(lead)
    company = core.enrich_company(lead)
    st.markdown(f"#### {esc(contact.full_name)} · {esc(contact.title)}")
    st.markdown("**Raw inbound lead** _(what actually came in)_")
    st.json(lead.model_dump())
    st.markdown("**Contact enrichment** · stub for Apollo / Hunter")
    st.json(contact.model_dump())
    st.markdown("**Company enrichment** · stub for Clay / Apollo")
    st.json(company.model_dump())


# ----------------------------------------------------------------------------- header
st.markdown('<div class="sircles-title">First<span>Touch</span></div>', unsafe_allow_html=True)
st.caption("Inbound lead to first-touch: enrich, debate, resolve, hand off - live.  ·  by Mark Takla")

# ----------------------------------------------------------------------------- sidebar
LEADS = {l.lead_id: l for l in core.fetch_new_leads()}
with st.sidebar:
    st.markdown('<div class="sircles-title">First<span>Touch</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="muted">by Mark Takla</div>', unsafe_allow_html=True)
    st.markdown('<div class="sec-h">Model</div>', unsafe_allow_html=True)
    model = st.selectbox("Groq model", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
                         label_visibility="collapsed")

    st.markdown('<div class="sec-h">Lead</div>', unsafe_allow_html=True)
    labels = {lid: f"{lid} - {LEADS[lid].name} ({LEADS[lid].company})" for lid in LEADS}
    options = list(labels.values()) + ["Custom lead..."]
    picked = st.radio("Lead", options, label_visibility="collapsed")

    custom_lead = None
    if picked == "Custom lead...":
        st.caption("Custom leads use thin fallback enrichment, so they often escalate.")
        c_name = st.text_input("Name", "Alex Doe")
        c_title = st.text_input("Title", "Head of Marketing")
        c_email = st.text_input("Email", "alex@acme.io")
        c_company = st.text_input("Company", "Acme")
        c_domain = st.text_input("Company domain", "acme.io")
        c_note = st.text_input("Inbound note", "Requested a demo.")
        custom_lead = core.RawLead(lead_id="CUSTOM", name=c_name, title=c_title, email=c_email,
                                   company=c_company, company_domain=c_domain or None,
                                   source="Custom (manual)", note=c_note)

    if custom_lead is not None:
        selected_lead = custom_lead
    else:
        sel_id = next(lid for lid, lbl in labels.items() if lbl == picked)
        selected_lead = LEADS[sel_id]

    if st.button(f"View {selected_lead.name}'s enrichment record",
                 use_container_width=True):
        show_enrichment_record(selected_lead)
    st.caption("Opens the stubbed Apollo / Clay / Hunter enrichment for this lead.")

    api_ok = bool(os.getenv("GROQ_API_KEY"))
    if not api_ok:
        st.error("GROQ_API_KEY not set. Add it to .env.")
    run = st.button("Run workflow", type="primary", use_container_width=True, disabled=not api_ok)

# ----------------------------------------------------------------------------- run
if run:
    lead = selected_lead

    llm = core.build_llm(model)
    agents = core.build_agents(llm)

    st.markdown('<div class="sec-h">Lead</div>', unsafe_allow_html=True)
    intake_box = st.empty()
    st.markdown('<div class="sec-h">Enrichment & ICP</div>', unsafe_allow_html=True)
    icp_box = st.empty()
    st.markdown('<div class="sec-h">Lead intelligence brief</div>', unsafe_allow_html=True)
    brief_ph = st.empty()

    st.markdown('<div class="sec-h">Debate: Advocate vs Skeptic</div>', unsafe_allow_html=True)
    cadv, cskep = st.columns(2)
    adv_ph, skep_ph = cadv.empty(), cskep.empty()
    rebuttal_box = st.container()
    res_box = st.container()
    out_box = st.container()
    crm_box = st.container()

    placeholders: dict = {"brief": brief_ph, "advocate": adv_ph, "skeptic": skep_ph}
    buffers: dict = {}

    def streamer(kind: str, agent: str):
        target = placeholders.get(kind)
        if target is None:
            return None
        accent = "adv" if "advocate" in kind else "skep" if "skeptic" in kind else "plain"
        target.markdown(thinking_card(agent, accent), unsafe_allow_html=True)
        buffers[kind] = ""

        def sink(token: str) -> None:
            buffers[kind] += token
            target.markdown(stream_card(agent, accent, buffers[kind]), unsafe_allow_html=True)

        return sink

    with st.spinner("Running the agent team..."):
        for ev in core.run_pipeline_events(lead, agents, streamer):
            if ev.kind == "intake":
                render_lead(intake_box, ev.state.raw_lead)
            elif ev.kind == "enriched":
                render_icp(icp_box, ev.state.icp)
            elif ev.kind == "brief":
                brief_ph.markdown(
                    f'<div class="card plain"><div class="kv">{esc(ev.payload)}</div></div>',
                    unsafe_allow_html=True)
            elif ev.kind == "position":
                p = ev.payload
                if p.agent == "Growth Advocate":
                    adv_ph.markdown(position_card(p, "adv"), unsafe_allow_html=True)
                else:
                    skep_ph.markdown(position_card(p, "skep"), unsafe_allow_html=True)
            elif ev.kind == "rebuttal_start":
                with rebuttal_box:
                    st.markdown('<div class="sec-h">Rebuttal round (contested)</div>',
                                unsafe_allow_html=True)
                    rc1, rc2 = st.columns(2)
                    placeholders["rebuttal_advocate"] = rc1.empty()
                    placeholders["rebuttal_skeptic"] = rc2.empty()
            elif ev.kind == "rebuttal":
                adv2, skep2 = ev.payload
                placeholders["rebuttal_advocate"].markdown(position_card(adv2, "adv"),
                                                           unsafe_allow_html=True)
                placeholders["rebuttal_skeptic"].markdown(position_card(skep2, "skep"),
                                                          unsafe_allow_html=True)
            elif ev.kind == "resolution":
                render_resolution(res_box, ev.payload)
            elif ev.kind == "outreach":
                render_outreach(out_box, ev.payload)
            elif ev.kind == "crm":
                with crm_box:
                    st.markdown('<div class="sec-h">CRM record (mock HubSpot)</div>',
                                unsafe_allow_html=True)
                    st.json(ev.payload.model_dump())
            elif ev.kind == "done":
                st.success("Workflow complete.")
else:
    st.info("Pick a lead in the sidebar and press **Run workflow** to watch the team debate it live.")
