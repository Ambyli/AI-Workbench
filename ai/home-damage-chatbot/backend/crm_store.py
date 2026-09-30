"""Mock CRM Case Store & Chatter Note Generator.

Manages CRM Cases opened through the disposition pipeline and provides rich
Chatter Note feed templates with embedded photo attachments and case workflow
status management.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CASES_FILE = Path(__file__).parent / "data" / "crm_cases.json"
_STORE: Dict[str, dict] = {}


def _tier_color_and_label(urgency: int, issue_type: str) -> tuple[str, str]:
    if issue_type.lower() == "electrical" and urgency < 6:
        urgency = max(urgency, 6)
    if urgency >= 8:
        return "#c0262b", "HIGH PRIORITY"
    if urgency >= 5:
        return "#e08a1e", "Elevated"
    return "#3a8d5b", "Standard"


def generate_chatter_note(case: dict, match_info: Optional[dict] = None) -> str:
    """Generate a clean, structured Salesforce/CRM Chatter Note text template with no emojis."""
    tier_color, tier_label = _tier_color_and_label(
        int(case.get("urgency", 5)), str(case.get("issue_type", "misc"))
    )
    customer_name = case.get("name") or case.get("account_name", "Customer")
    acct_num = (match_info or {}).get("account_number") or case.get("account_number", "ZEO-UNVERIFIED")
    address = case.get("account_address") or case.get("address", "Address not provided")
    contact = case.get("contact") or case.get("email", "Not provided")
    issue_type = str(case.get("issue_type", "misc")).capitalize()
    urgency = case.get("urgency", 5)
    summary = case.get("summary") or f"{customer_name} reported a {issue_type} issue (urgency {urgency}/10)."
    verbatim_list = case.get("verbatim") or []
    attachments = case.get("attachments") or []
    damage_pointer = case.get("damage_pointer")

    # Extract diagnostic facts based on the chatbot flow
    facts_html = ""
    try:
        from . import email_render
        if "issue_type" in case:
            facts = email_render._facts(case)
            if facts:
                facts_html = """  <div style="margin-bottom: 12px;">\n    <strong>ISSUE DIAGNOSTIC FACTS:</strong><br>\n"""
                for label, val in facts:
                    facts_html += f"    &bull; <strong>{label}:</strong> {val}<br>\n"
                facts_html += "  </div>\n"
    except Exception:
        pass

    html = f"""
<div class="chatter-post" style="font-family: Arial, Helvetica, sans-serif; font-size: 13px; line-height: 1.5; color: #222; background: #fff; padding: 18px; border: 1px solid #d8dde6; border-radius: 4px;">
  <div style="font-size: 12px; color: #555; border-bottom: 1px solid #e5e5e5; padding-bottom: 8px; margin-bottom: 12px;">
    <strong>Zeo Service Bot (Automated Intake)</strong> &middot; Case #{case.get('case_id', 'NEW')} &middot; Account #{acct_num}
  </div>

  <div style="margin-bottom: 12px;">
    <strong>SERVICE INTAKE CASE NOTE</strong><br>
    <strong>Category:</strong> {issue_type} | <strong>Priority Tier:</strong> {tier_label} (Urgency: {urgency}/10)<br>
    <strong>Summary:</strong> {summary}
  </div>

  <div style="margin-bottom: 12px;">
    <strong>INTAKE DETAILS:</strong><br>
    &bull; <strong>Account Number:</strong> {acct_num}<br>
    &bull; <strong>Requester:</strong> {customer_name}<br>
    &bull; <strong>Contact:</strong> {contact}<br>
    &bull; <strong>Service Address:</strong> {address}<br>
    &bull; <strong>Disposition Routing:</strong> {case.get('routed_to', 'service-team@zeoenergy.com')}
  </div>
{facts_html}"""

    if verbatim_list:
        html += """
  <div style="margin-bottom: 12px;">
    <strong>CUSTOMER STATEMENT (Untrusted Verbatim):</strong><br>
"""
        for v in verbatim_list:
            html += f"    <em>\"{v}\"</em><br>\n"
        html += "  </div>\n"

    if damage_pointer or attachments:
        html += """
  <div style="margin-top: 14px; border-top: 1px solid #eee; padding-top: 10px;">
    <strong>ATTACHED PHOTOS & DAMAGE LOCATION:</strong>
    <div style="display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px;">
"""
        if damage_pointer:
            html += f"""
      <div style="border: 1px solid #ccc; border-radius: 4px; padding: 4px; width: 180px; background: #fafafa;">
        <img src="/uploads/{damage_pointer}" alt="Damage Map Pointer" style="width: 100%; height: 100px; object-fit: cover; display: block;" onclick="openPhotoModal('/uploads/{damage_pointer}', 'Damage Map Pointer')" />
        <div style="font-size: 11px; color: #444; margin-top: 4px;">Damage Location Pointer</div>
      </div>
"""
        for att in attachments:
            fname = att.get("filename") if isinstance(att, dict) else att
            if fname and fname != damage_pointer:
                html += f"""
      <div style="border: 1px solid #ccc; border-radius: 4px; padding: 4px; width: 180px; background: #fafafa;">
        <img src="/uploads/{fname}" alt="{fname}" onerror="this.src='/assets/suburban_house.png'" style="width: 100%; height: 100px; object-fit: cover; display: block;" onclick="openPhotoModal('/uploads/{fname}', '{fname}')" />
        <div style="font-size: 11px; color: #444; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{fname}</div>
      </div>
"""
        html += "    </div>\n  </div>\n"

    html += """
  <div style="font-size: 11px; color: #777; margin-top: 12px; border-top: 1px solid #f0f0f0; padding-top: 6px;">
    <em>Notice: Generated automatically by service intake bot. Images signature-validated, EXIF stripped.</em>
  </div>
</div>
"""
    return html.strip()


def seed_crm_cases() -> None:
    """Seed initial realistic CRM cases matching seeded emails."""
    seeds = [
        {
            "case_id": "CASE-MOCK-001",
            "created_at": "2026-06-19T13:42:00+00:00",
            "account_number": "ZEO-MOCK-001",
            "customer_name": "Jane Doe",
            "service_address": "100 Solar Way, Tampa, FL 33601",
            "contact": "jane.doe@example.com",
            "issue_type": "roof",
            "issue_label": "Roof Damage",
            "urgency": 8,
            "tier_label": "HIGH PRIORITY",
            "tier_color": "#c0262b",
            "status": "in_review",
            "routed_to": "service-team@zeoenergy.com",
            "match_confidence": 0.95,
            "summary": "Jane Doe reported an active roof leak near chimney slope (urgency 8/10). Active leak confirmed; attic accessible.",
            "verbatim": ["Water is dripping into the upstairs hallway whenever it rains hard. There's a brown stain spreading across the ceiling and I'm worried about the drywall."],
            "damage_pointer": "suburban_house.png",
            "attachments": [
                {"filename": "suburban_house.png", "size_kb": 848, "label": "Front slope damage pointer"},
                {"filename": "roof_chimney_1.jpg", "size_kb": 412, "label": "Chimney flashing separation"},
            ],
            "email_id": "1a6fe8afde82",
        },
        {
            "case_id": "CASE-MOCK-002",
            "created_at": "2026-06-19T11:08:00+00:00",
            "account_number": "ZEO-MOCK-002",
            "customer_name": "John Smith",
            "service_address": "200 Sunny Ln, Clearwater, FL 33755",
            "contact": "john.smith@example.com",
            "issue_type": "electrical",
            "issue_label": "Electrical / Breakers",
            "urgency": 9,
            "tier_label": "HIGH PRIORITY",
            "tier_color": "#c0262b",
            "status": "pending_disposition",
            "routed_to": "nonstandard@zeoenergy.com",
            "match_confidence": 0.98,
            "summary": "John Smith reported a critical electrical failure (urgency 9/10). Main subpanel popped and entire home is without power.",
            "verbatim": ["Half the house went dark right after the panel made a loud popping sound. None of the breakers will reset."],
            "damage_pointer": None,
            "attachments": [
                {"filename": "electrical_panel.jpg", "size_kb": 520, "label": "Tripped breaker subpanel"},
            ],
            "email_id": "43bf3470f49b",
        },
        {
            "case_id": "CASE-MOCK-005",
            "created_at": "2026-06-18T16:55:00+00:00",
            "account_number": "ZEO-MOCK-005",
            "customer_name": "Jordan Lee",
            "service_address": "500 Electric Ave, Lakeland, FL 33801",
            "contact": "jordan.lee@example.com",
            "issue_type": "solar",
            "issue_label": "Solar Production",
            "urgency": 3,
            "tier_label": "Standard",
            "tier_color": "#3a8d5b",
            "status": "assigned",
            "routed_to": "performance-team@zeoenergy.com",
            "match_confidence": 0.92,
            "summary": "Jordan Lee reported a solar production discrepancy (urgency 3/10). Routed to performance monitoring.",
            "verbatim": ["My production has been noticeably lower than last summer for the past couple of weeks. Wanted to flag it."],
            "damage_pointer": None,
            "attachments": [],
            "email_id": "8b2bb76f4f82",
        },
        {
            "case_id": "CASE-UNVERIFIED-MISC",
            "created_at": "2026-06-18T09:21:00+00:00",
            "account_number": "ZEO-UNVERIFIED",
            "customer_name": "Sam Sample",
            "service_address": "400 Battery Ct, Sarasota, FL 34236",
            "contact": "sam.sample@example.com",
            "issue_type": "misc",
            "issue_label": "Miscellaneous Damage",
            "urgency": 5,
            "tier_label": "Elevated",
            "tier_color": "#e08a1e",
            "status": "pending_disposition",
            "routed_to": "intake-review@zeoenergy.com",
            "match_confidence": 0.0,
            "summary": "Sam Sample reported a falling branch damaged backyard ground-mount array (urgency 5/10). Requester unverified in CRM.",
            "verbatim": ["A big branch came down in the storm and bent the frame on two of the ground panels out back."],
            "damage_pointer": None,
            "attachments": [
                {"filename": "ground_mount_branch.jpg", "size_kb": 610, "label": "Ground array damage"},
            ],
            "email_id": "d7ca325229a5",
        },
    ]

    for seed in seeds:
        seed["chatter_note"] = generate_chatter_note(seed, {"account_number": seed["account_number"]})
        _STORE[seed["case_id"]] = seed


def get_all_cases() -> List[dict]:
    if not _STORE:
        seed_crm_cases()
    return list(reversed(list(_STORE.values())))


def get_case(case_id: str) -> Optional[dict]:
    if not _STORE:
        seed_crm_cases()
    return _STORE.get(case_id)


def record_case(case_data: dict) -> dict:
    if not _STORE:
        seed_crm_cases()
    case_id = case_data.get("case_id") or f"CASE-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    case_data["case_id"] = case_id
    if "created_at" not in case_data:
        case_data["created_at"] = datetime.now(timezone.utc).isoformat()
    if "status" not in case_data:
        case_data["status"] = "pending_disposition"
    if "chatter_note" not in case_data:
        case_data["chatter_note"] = generate_chatter_note(case_data)
    
    _STORE[case_id] = case_data
    return case_data


def update_status(case_id: str, new_status: str) -> Optional[dict]:
    if not _STORE:
        seed_crm_cases()
    c = _STORE.get(case_id)
    if c:
        c["status"] = new_status
        return c
    return None
