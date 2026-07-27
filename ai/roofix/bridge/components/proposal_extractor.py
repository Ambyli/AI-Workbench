"""
PROPOSAL EXTRACTOR — pulls Phoenix-writable fields out of the scraper's response.

Input:  the JSON dict returned by roofix-scraper's `GET /proposal/{id}` — an
        object with an ``init_data`` list of Bubble/Roofix documents.

Output: an ``ExtractedProposal`` dataclass with the fields Phoenix's
        create_project will need, plus an ``is_accepted`` boolean derived
        from the acceptance signals we discovered in T4.

Signals (T4 findings, ranked by reliability):

  Primary — HIC (Home Improvement Contract) record exists on the order doc.
            This is the actual contract; if the customer signed, this exists.

  Secondary — Job record exists. Only created after HIC signing, and only if
              the project is going into production. Redundant with HIC in most
              cases but covers the tiny window where HIC exists but Job hasn't
              been spun up yet.

  Confirmation — manual_loan_docs_signed = "yes" (loan flow only), ntp_received
                 = "yes", warranty record present, msa_list non-empty. None of
                 these are universal but together they raise confidence.

The extractor is pure — no I/O, no external calls. It's meant to be run against
the scraper's return value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


_LOOKUP_SEP = "__LOOKUP__"

# Order doc's Bubble type as it appears in the scraper's init_data entries.
_ORDER_TYPE = "custom.order1"


@dataclass
class AcceptanceSignals:
    """Individual sub-signals that go into the ``is_accepted`` decision.

    Kept as a separate object so downstream code (and log lines) can inspect
    *why* something was ruled accepted or not.
    """
    has_hic: bool = False
    has_job: bool = False
    has_warranty: bool = False
    loan_signed: bool = False
    ntp_received: bool = False
    msa_count: int = 0

    def as_dict(self) -> dict:
        return {
            "has_hic": self.has_hic,
            "has_job": self.has_job,
            "has_warranty": self.has_warranty,
            "loan_signed": self.loan_signed,
            "ntp_received": self.ntp_received,
            "msa_count": self.msa_count,
        }


@dataclass
class ExtractedProposal:
    """The Phoenix-relevant slice of a Roofix proposal.

    All fields default to None / False / 0 so a partial extraction (order doc
    with missing fields) still yields a usable object. ``ok`` is False only
    when the order doc itself couldn't be located.
    """
    ok: bool = False
    error: Optional[str] = None

    # ── Identity ──────────────────────────────────────────────────────────
    roofix_project_id: Optional[str] = None
    external_ref: Optional[str] = None

    # ── Customer ──────────────────────────────────────────────────────────
    display_text: Optional[str] = None
    customer_name: Optional[str] = None
    address: Optional[str] = None

    # ── Money ─────────────────────────────────────────────────────────────
    contract_price: Optional[float] = None
    staging_price: Optional[float] = None
    markup: Optional[float] = None

    # ── Config ────────────────────────────────────────────────────────────
    funding_type: Optional[str] = None
    financing_provider: Optional[str] = None
    trade: Optional[str] = None
    project_type: Optional[str] = None
    steep_slope_product: Optional[str] = None

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

    # ── Progress ──────────────────────────────────────────────────────────
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
    original string. None input → None output.
    """
    if not isinstance(value, str):
        return None
    idx = value.find(_LOOKUP_SEP)
    if idx == -1:
        return value or None
    return value[idx + len(_LOOKUP_SEP):] or None


def _split_display_text(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split ``"Customer Name - Address"`` into (name, address).

    Splits on the FIRST ' - ' so addresses containing hyphens survive intact.
    If there's no separator, returns (whole_text, None).
    """
    if not text:
        return None, None
    parts = text.split(" - ", 1)
    if len(parts) == 1:
        return parts[0].strip() or None, None
    return parts[0].strip() or None, parts[1].strip() or None


def _find_order_doc(init_data: list) -> Optional[dict]:
    """Return the ``data`` dict of the first ``custom.order1`` entry, or None."""
    for entry in init_data or []:
        if entry.get("type") == _ORDER_TYPE:
            data = entry.get("data")
            if isinstance(data, dict):
                return data
    return None


def _num(value: Any) -> Optional[float]:
    """Coerce to float. Returns None on missing / non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Public API ─────────────────────────────────────────────────────────────

def extract_proposal(scraper_response: dict) -> ExtractedProposal:
    """Extract the Phoenix-writable proposal fields from a scraper response.

    Never raises — failures set ``ok=False`` and populate ``error``.
    """
    init_data = (scraper_response or {}).get("init_data")
    if not init_data:
        return ExtractedProposal(
            ok=False,
            error="scraper response has no init_data (empty or missing)",
        )

    order = _find_order_doc(init_data)
    if not order:
        return ExtractedProposal(
            ok=False,
            error=f"no {_ORDER_TYPE} entry found in init_data (login wall? scrape too short?)",
        )

    # ── Identity + customer ──────────────────────────────────────────────
    display_text = order.get("display_text")
    customer_name, address = _split_display_text(display_text)

    # ── Acceptance signals ────────────────────────────────────────────────
    hic_ref = _lookup_id(order.get("hic_custom_hic"))
    job_ref = _lookup_id(order.get("job_custom_job1"))
    warranty_ref = _lookup_id(order.get("warranty_custom_warranty"))

    signals = AcceptanceSignals(
        has_hic=bool(hic_ref),
        has_job=bool(job_ref),
        has_warranty=bool(warranty_ref),
        loan_signed=order.get("manual_loan_docs_signed_option_redeck") == "yes",
        ntp_received=order.get("ntp_received_option_redeck") == "yes",
        msa_count=len(order.get("msa_list_custom_msa") or []),
    )
    # Primary rule (T4): HIC OR Job present ⇒ accepted. Both funding paths
    # (loan + cash) route through HIC, so HIC is the universal signal; Job
    # covers the brief post-HIC / pre-Job window.
    is_accepted = signals.has_hic or signals.has_job

    return ExtractedProposal(
        ok=True,
        roofix_project_id=order.get("_id"),
        external_ref=order.get("external_project_id_text"),
        display_text=display_text,
        customer_name=customer_name,
        address=address,
        contract_price=_num(order.get("price__final__number")),
        staging_price=_num(order.get("staging_price_number")),
        markup=_num(order.get("markup__final__number") or order.get("rfx_markup_number")),
        funding_type=order.get("funding1_option_funding"),
        financing_provider=order.get("financing_provider_option_loan_provider"),
        trade=order.get("trade_option_trade"),
        project_type=order.get("type_option_type__estimate_"),
        steep_slope_product=order.get("steep_slope_product_option_steep_slope_products"),
        sales_rep_ref=_lookup_id(order.get("sales_rep_user")),
        estimator_ref=_lookup_id(order.get("estimator_user")),
        office_ref=_lookup_id(order.get("office_custom_office")),
        homeowner_ref=_lookup_id(order.get("homeowner_custom_homeowner")),
        hic_ref=hic_ref,
        job_ref=job_ref,
        warranty_ref=warranty_ref,
        is_accepted=is_accepted,
        acceptance_signals=signals,
        stage_completed_internal=order.get(
            "pts_trigger_type_completed__internal__option_progress_tracker_stage"),
        stage_upcoming_internal=order.get(
            "pts_trigger_type_upcoming__internal__option_progress_tracker_stage"),
        stage_completed_external=order.get(
            "pts_trigger_type_completed__external__option_progress_tracker_stage"),
    )
