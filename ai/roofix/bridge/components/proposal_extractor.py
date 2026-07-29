"""
PROPOSAL EXTRACTOR — pulls Phoenix-writable fields from the scraper output.

Input:  the JSON dict returned by ``RoofixScraperClient.get_proposal(tracking_url)``
        (``ai/roofix/bridge/components/roofix_scraper_client.py``). That client
        POSTs to ``interceptor-api``'s ``/capture`` under the hood and reshapes
        the response into the legacy scraper's ``/proposal/{id}`` dict shape.
        The extractor reads from TWO fields on that response:

          ``init_data`` — Bubble's page-hydration payload. The CURRENT project's
             ``custom.order1`` doc lives here. This is authoritative for prices,
             funding type, progress-tracker stages, and other order-level fields.

          ``mget_docs`` — aggregated ``/elasticsearch/mget`` docs, one entry per
             related record: homeowner (customer + full address), hic (signature
             + executed status), job1 (actual contract price + install date),
             warranty, estimate. Notably, mget does NOT return the current
             project's own order1 doc — it returns the homeowner's OTHER past
             orders. That's why init_data is still required.

Output: an ``ExtractedProposal`` dataclass with the fields Phoenix's
        create_project needs, plus an ``is_accepted`` boolean derived
        from three independent acceptance signals.

Why the hybrid over just init_data:
    init_data alone lacks the customer address (zip, city, state, street), the
    HIC executed status + signature, the actual signed contract price, install
    date, job status, and shingle color. mget carries all of that on separate
    docs. Reading from both gives us the complete picture.

Acceptance signals (any ONE is sufficient — three independent paths):
    1. custom.hic exists AND its status_option_contingency == "executed".
       This is the gold standard: the physical contract has been signed.
    2. custom.job1 exists AND status_option_job_status is set.
       Job records are created only after HIC signing and represent an
       executable project. Covers the tiny window between HIC signing and
       when the primary signal reflects it.
    3. custom.homeowner.stage_option_type__contact_ == "customer".
       Roofix's own CRM classification. "opportunity" = not yet accepted.

Any single signal is enough; using three independent paths is robust against
one field's behaviour changing.

The extractor is pure — no I/O, no external calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


_LOOKUP_SEP = "__LOOKUP__"
_PROJECT_URL_RE = re.compile(r"/project/([0-9]+x[0-9]+)")

# Roofix doc types the extractor reads.
_ORDER_TYPE = "custom.order1"
_HOMEOWNER_TYPE = "custom.homeowner"
_HIC_TYPE = "custom.hic"
_JOB_TYPE = "custom.job1"
_WARRANTY_TYPE = "custom.warranty"
_ESTIMATE_TYPE = "custom.estimate1"


@dataclass
class AcceptanceSignals:
    """Individual sub-signals that go into ``is_accepted``.

    Kept as a separate object so downstream code and log lines can inspect
    *why* something was ruled accepted or not.
    """
    # Primary
    hic_present: bool = False
    hic_executed: bool = False           # hic exists AND status == "executed"
    hic_signature_present: bool = False

    # Secondary
    job_present: bool = False
    job_status: Optional[str] = None     # e.g. "completed", "in_progress"

    # Independent
    homeowner_stage: Optional[str] = None  # "customer" | "opportunity" | ...

    # Diagnostic
    warranty_present: bool = False

    def as_dict(self) -> dict:
        return {
            "hic_present": self.hic_present,
            "hic_executed": self.hic_executed,
            "hic_signature_present": self.hic_signature_present,
            "job_present": self.job_present,
            "job_status": self.job_status,
            "homeowner_stage": self.homeowner_stage,
            "warranty_present": self.warranty_present,
        }


@dataclass
class ExtractedProposal:
    """The Phoenix-writable slice of a Roofix proposal.

    All fields default to None / False / 0 so a partial extraction (some
    docs missing) still yields a usable object. ``ok`` is False only when
    no ``custom.order1`` doc could be located — that's the minimum viable
    identity.
    """
    ok: bool = False
    error: Optional[str] = None

    # ── Identity ──────────────────────────────────────────────────────────
    roofix_project_id: Optional[str] = None
    external_ref: Optional[str] = None      # short human ref ("5YS73T")
    display_text: Optional[str] = None      # "Name - Address" combined

    # ── Customer (from custom.homeowner) ──────────────────────────────────
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    # ── Address (from custom.homeowner) ───────────────────────────────────
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None             # "Ohio"
    state_abbr: Optional[str] = None        # "OH"
    zip_code: Optional[str] = None
    portal_code: Optional[str] = None       # Roofix portal code (not USPS zip)

    # ── Money (proposal-side, from custom.order1) ─────────────────────────
    estimated_price: Optional[float] = None    # order.price__final__number
    staging_price: Optional[float] = None
    markup: Optional[float] = None

    # ── Money (contract-side, from custom.job1) ───────────────────────────
    actual_contract_price: Optional[float] = None   # job.contract_price_number

    # ── Config (from custom.order1) ───────────────────────────────────────
    funding_type: Optional[str] = None              # proposed
    financing_provider: Optional[str] = None
    trade: Optional[str] = None
    project_type: Optional[str] = None
    steep_slope_product: Optional[str] = None

    # ── Config (from custom.job1) ─────────────────────────────────────────
    job_funding_source: Optional[str] = None        # actual funding used
    shingle_color: Optional[str] = None
    install_date_ms: Optional[int] = None
    install_scheduled_date_ms: Optional[int] = None
    job_status: Optional[str] = None

    # ── Related Bubble records (opaque LOOKUP target ids) ─────────────────
    sales_rep_ref: Optional[str] = None
    estimator_ref: Optional[str] = None
    office_ref: Optional[str] = None
    homeowner_ref: Optional[str] = None
    hic_ref: Optional[str] = None
    job_ref: Optional[str] = None
    warranty_ref: Optional[str] = None

    # ── Acceptance ────────────────────────────────────────────────────────
    is_accepted: bool = False
    acceptance_signals: AcceptanceSignals = field(default_factory=AcceptanceSignals)

    # ── Progress tracker (from custom.order1) ─────────────────────────────
    stage_completed_internal: Optional[str] = None
    stage_upcoming_internal: Optional[str] = None
    stage_completed_external: Optional[str] = None

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "acceptance_signals"}
        d["acceptance_signals"] = self.acceptance_signals.as_dict()
        return d


# ── Helpers ────────────────────────────────────────────────────────────────

def _lookup_id(value: Any) -> Optional[str]:
    """Strip the ``<something>__LOOKUP__`` prefix from a Bubble lookup string.

    Returns the id portion if the value looks like a LOOKUP, otherwise the
    original string. None / empty / non-string → None.
    """
    if not isinstance(value, str):
        return None
    if not value:
        return None
    idx = value.find(_LOOKUP_SEP)
    if idx == -1:
        return value
    tail = value[idx + len(_LOOKUP_SEP):]
    return tail or None


def _first_source_by_type(mget_docs: list, doc_type: str) -> Optional[dict]:
    """Return the ``_source`` dict of the first mget doc with the given _type.

    mget fires multiple times per page load with overlapping doc sets — we
    take the first entry per type. Bubble consistently returns the same
    document for the same id across mget calls, so first-wins is stable.

    WARNING: for doc types that can have multiple distinct entries in one
    scrape (e.g. ``custom.order1`` — a homeowner with multiple past orders
    returns all of them), use ``_source_by_type_and_id`` instead so you can
    target a specific record.
    """
    for d in mget_docs or []:
        if not isinstance(d, dict):
            continue
        if d.get("_type") == doc_type:
            src = d.get("_source")
            if isinstance(src, dict):
                return src
    return None


def _source_by_type_and_id(mget_docs: list, doc_type: str,
                           doc_id: Optional[str]) -> Optional[dict]:
    """Find a specific mget doc by (_type, _id). Returns None if either is
    unmatched or ``doc_id`` is falsy."""
    if not doc_id:
        return None
    for d in mget_docs or []:
        if not isinstance(d, dict):
            continue
        if d.get("_type") == doc_type and d.get("_id") == doc_id:
            src = d.get("_source")
            if isinstance(src, dict):
                return src
    return None


def _init_data_order(init_data: list, project_id: Optional[str]) -> dict:
    """Find the ``custom.order1`` entry in ``init_data`` matching ``project_id``.

    init_data entries are shaped ``{id, type, data, version}`` (Bubble's own
    envelope — different from mget's elasticsearch envelope). The ``data``
    dict is the same shape as an mget ``_source`` for the same doc type.

    If ``project_id`` is unset, falls back to the first order1 entry. If no
    order1 is present at all, returns an empty dict (the extractor treats
    this as "order-side fields unavailable", not a hard failure).
    """
    first_order: dict = {}
    for entry in init_data or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != _ORDER_TYPE:
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        if project_id and data.get("_id") == project_id:
            return data
        if not first_order:
            first_order = data
    return first_order


def _num(value: Any) -> Optional[float]:
    """Coerce to float. Returns None on missing / non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    """Coerce to int (used for timestamps). Returns None on missing / non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_id_from_url(url: Optional[str]) -> Optional[str]:
    """Extract the Bubble project id from a ``roofix.io/project/{id}`` URL.

    Roofix skips the ``custom.order1`` doc in mget for proposals that haven't
    been accepted yet — so the URL is the reliable identity source. Returns
    None only if the URL isn't a project URL.
    """
    if not url:
        return None
    m = _PROJECT_URL_RE.search(url)
    return m.group(1) if m else None


# ── Public API ─────────────────────────────────────────────────────────────

def extract_proposal(scraper_response: dict) -> ExtractedProposal:
    """Extract the Phoenix-writable proposal fields from a scraper response.

    Never raises — failures set ``ok=False`` and populate ``error``.
    """
    if not scraper_response:
        return ExtractedProposal(ok=False, error="empty scraper response")

    mget_docs = scraper_response.get("mget_docs") or []
    init_data = scraper_response.get("init_data") or []

    if not mget_docs and not init_data:
        return ExtractedProposal(
            ok=False,
            error="scraper response has neither init_data nor mget_docs "
                  "(empty or missing)",
        )

    # ── Identity anchor ──────────────────────────────────────────────────
    # URL is the reliable anchor. mget can return other orders (past ones
    # by the same homeowner) and init/data may or may not carry the current
    # order1 depending on state.
    url_project_id = _project_id_from_url(scraper_response.get("url"))

    # ── Order1 — from init_data (authoritative for the current project) ──
    # init_data's entries are shaped {id, type, data, version}. We want the
    # data of the one where type==custom.order1 AND id==url_project_id.
    order = _init_data_order(init_data, url_project_id)

    # ── Everything else — from mget_docs ─────────────────────────────────
    homeowner = _first_source_by_type(mget_docs, _HOMEOWNER_TYPE) or {}
    hic = _first_source_by_type(mget_docs, _HIC_TYPE)            # None if absent
    job = _first_source_by_type(mget_docs, _JOB_TYPE)            # None if absent
    warranty = _first_source_by_type(mget_docs, _WARRANTY_TYPE)  # None if absent

    # Identity: prefer order1._id, then URL. Unaccepted proposals with no
    # order1 in init_data still resolve via URL.
    roofix_project_id = order.get("_id") or url_project_id
    if not roofix_project_id and not homeowner:
        return ExtractedProposal(
            ok=False,
            error="no order1 doc, no homeowner, and no project id in URL "
                  "(login wall? scrape too short?)",
        )

    # ── Acceptance signals ────────────────────────────────────────────────
    hic_status = hic.get("status_option_contingency") if hic else None
    hic_signature_present = bool(hic and hic.get("signature_url_text"))
    job_status = job.get("status_option_job_status") if job else None
    homeowner_stage = homeowner.get("stage_option_type__contact_")

    signals = AcceptanceSignals(
        hic_present=hic is not None,
        hic_executed=(hic_status == "executed"),
        hic_signature_present=hic_signature_present,
        job_present=job is not None,
        job_status=job_status,
        homeowner_stage=homeowner_stage,
        warranty_present=warranty is not None,
    )

    # Three-way OR: any single signal is sufficient. This is robust against
    # any one field's behaviour changing on Roofix's side.
    is_accepted = (
        signals.hic_executed
        or bool(signals.job_status)
        or signals.homeowner_stage == "customer"
    )

    return ExtractedProposal(
        ok=True,

        # Identity — project id from order1 if present, else from URL
        roofix_project_id=roofix_project_id,
        external_ref=order.get("external_project_id_text"),
        display_text=order.get("display_text"),

        # Customer
        first_name=homeowner.get("first_name_text"),
        last_name=homeowner.get("last_name_text"),
        full_name=homeowner.get("full_name_text"),
        email=homeowner.get("email_text"),
        # Bubble's field is misspelled as "phone_nmber_text" — see it in the docs.
        phone=homeowner.get("phone_nmber_text"),

        # Address
        street_address=homeowner.get("street_address_text"),
        city=homeowner.get("city_text"),
        state=homeowner.get("state_text"),
        state_abbr=homeowner.get("state_abbr_text"),
        zip_code=homeowner.get("zip_text"),
        portal_code=homeowner.get("portal_code_text"),

        # Money — proposal side
        estimated_price=_num(order.get("price__final__number")),
        staging_price=_num(order.get("staging_price_number")),
        markup=_num(order.get("markup__final__number") or order.get("rfx_markup_number")),

        # Money — contract side (from job doc)
        actual_contract_price=_num(job.get("contract_price_number")) if job else None,

        # Config — order side
        funding_type=order.get("funding1_option_funding"),
        financing_provider=order.get("financing_provider_option_loan_provider"),
        trade=order.get("trade_option_trade"),
        project_type=order.get("type_option_type__estimate_"),
        steep_slope_product=order.get("steep_slope_product_option_steep_slope_products"),

        # Config — job side
        job_funding_source=job.get("funding_source_option_funding") if job else None,
        shingle_color=job.get("shingle_color_v2_text") if job else None,
        install_date_ms=_int(job.get("install_date_date")) if job else None,
        install_scheduled_date_ms=_int(job.get("install_scheduled_date_date")) if job else None,
        job_status=job_status,

        # Related Bubble records
        sales_rep_ref=_lookup_id(order.get("sales_rep_user")),
        estimator_ref=_lookup_id(order.get("estimator_user")),
        office_ref=_lookup_id(order.get("office_custom_office")),
        homeowner_ref=_lookup_id(order.get("homeowner_custom_homeowner")) or (homeowner.get("_id") if homeowner else None),
        hic_ref=hic.get("_id") if hic else None,
        job_ref=job.get("_id") if job else None,
        warranty_ref=warranty.get("_id") if warranty else None,

        # Acceptance
        is_accepted=is_accepted,
        acceptance_signals=signals,

        # Progress
        stage_completed_internal=order.get(
            "pts_trigger_type_completed__internal__option_progress_tracker_stage"),
        stage_upcoming_internal=order.get(
            "pts_trigger_type_upcoming__internal__option_progress_tracker_stage"),
        stage_completed_external=order.get(
            "pts_trigger_type_completed__external__option_progress_tracker_stage"),
    )
