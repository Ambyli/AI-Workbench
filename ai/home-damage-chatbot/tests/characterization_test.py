"""Characterization tests (mock LLM). Pin CURRENT behavior before Stage-1 changes.

Build Tree Framework, Phase R gaps G1-G5. These lock down behavior that the
existing suites only exercise indirectly, so later refactors/optimizations can
prove they preserved it. Run: USE_MOCK_LLM=1 venv/bin/python characterization_test.py
"""
import io
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["USE_MOCK_LLM"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from PIL import Image
from fastapi.testclient import TestClient

from backend.main import app
from backend import email_render, seed, upload
from backend.email_render import _tier
from backend.safety import is_safety_concern
from backend.schemas import IssueType

# Clean, deterministic store for any email-producing flow.
email_render._STORE.clear()
seed.seed_if_empty()
email_render._persist()

c = TestClient(app)
c.__enter__()


def chat(sid, mode, msg="", atts=None):
    r = c.post("/api/chat", json={"session_id": sid, "mode": mode, "message": msg,
                                  "attachments": atts or []})
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# G1 — email_render._tier() urgency -> (label, color), incl. electrical floor
# ---------------------------------------------------------------------------
def test_tier_mapping():
    print("G1: tier mapping (urgency -> label/color)...")
    # Non-electrical bands.
    assert _tier(1, IssueType.ROOF) == ("Standard", "#3a8d5b")
    assert _tier(4, IssueType.ROOF) == ("Standard", "#3a8d5b")
    assert _tier(5, IssueType.ROOF) == ("Elevated", "#e08a1e")
    assert _tier(7, IssueType.ROOF) == ("Elevated", "#e08a1e")
    assert _tier(8, IssueType.ROOF) == ("HIGH PRIORITY", "#c0262b")
    assert _tier(10, IssueType.MISC) == ("HIGH PRIORITY", "#c0262b")
    # Electrical floors at Elevated: urgency 1-4 is lifted to 6 -> "Elevated".
    assert _tier(1, IssueType.ELECTRICAL) == ("Elevated", "#e08a1e"), "electrical must floor at Elevated"
    assert _tier(4, IssueType.ELECTRICAL) == ("Elevated", "#e08a1e")
    assert _tier(5, IssueType.ELECTRICAL) == ("Elevated", "#e08a1e")
    # Electrical still escalates to HIGH at >=8.
    assert _tier(9, IssueType.ELECTRICAL) == ("HIGH PRIORITY", "#c0262b")
    print("  ok: bands Standard(<5)/Elevated(5-7)/HIGH(>=8); electrical floors at 6")


# ---------------------------------------------------------------------------
# G5 — safety.is_safety_concern keyword matrix (NT2: pin before extending)
# ---------------------------------------------------------------------------
def test_safety_matrix():
    print("G5: safety classifier matrix...")
    fires = [
        "I smell gas in the house",
        "there is smoke coming from the panel",
        "the wire is sparking",
        "someone got a shock",
        "I think there's carbon monoxide",
        "call 911",
        "my roof is on fire",
        "there was an explosion",
    ]
    for m in fires:
        assert is_safety_concern(m) is True, f"should fire: {m!r}"
    # Must NOT fire on benign intake language that shares no hazard stem.
    quiet = [
        "my roof shingles are loose",
        "the inverter shows a warning light",
        "the water heater is leaking",
        "",
        "just a routine question about my panels",
    ]
    for m in quiet:
        assert is_safety_concern(m) is False, f"should stay quiet: {m!r}"
    # CHARACTERIZED acceptable false positives: the classifier is intentionally
    # conservative (GOAL: false positives acceptable, false negatives are not).
    # "fired" contains the "fire" stem, so it fires. NT2 forbids weakening the
    # classifier to chase this, since narrowing risks a false negative on real
    # "fire". Pinned here so nobody "fixes" it into a gap.
    conservative_fp = ["I fired my previous contractor"]
    for m in conservative_fp:
        assert is_safety_concern(m) is True, f"characterized conservative FP should still fire: {m!r}"
    print("  ok: fires on hazards; quiet on benign; 'fired' is a pinned conservative FP")


# ---------------------------------------------------------------------------
# G3 — Solar and Misc issue types drive end-to-end to a rendered email
# ---------------------------------------------------------------------------
def _run_common(sid, issue_reply):
    chat(sid, "standard")                                  # greeting
    chat(sid, "standard", "Pat Tester")                    # name
    chat(sid, "standard", "Pat Tester")                    # account_name
    chat(sid, "standard", "500 Test Blvd, Tampa, FL")      # account_address
    chat(sid, "standard", "pat@example.com")               # contact
    chat(sid, "standard", issue_reply)                     # issue_type
    chat(sid, "standard", "6")                             # urgency
    chat(sid, "standard", "skip")                          # third_parties


def test_solar_e2e():
    print("G3a: solar issue end-to-end...")
    sid = "char_solar"
    _run_common(sid, "Solar not producing")
    # Mock enum matcher keys off the option token (not the button label).
    chat(sid, "standard", "low_production")                # solar_status
    chat(sid, "standard", "err E013")                      # inverter_error_code
    # Validation gate: a non-answer to the required verbatim description re-prompts.
    reprompt = chat(sid, "standard", "idk")
    assert reprompt["state"] == "collect", "non-answer to required description should re-prompt (G2)"
    chat(sid, "standard", "production dropped to almost nothing since the storm")
    chat(sid, "standard", "No")                            # callback_requested
    conf = chat(sid, "standard", "skip")                   # photos -> confirm
    assert conf["state"] == "confirm", f"expected confirm, got {conf['state']}"
    done = chat(sid, "standard", "Yes, send it")
    assert done["done"] and done["email_id"], "solar flow should finish with an email"
    rec = c.get("/api/emails/" + done["email_id"]).json()
    assert rec["issue_type"] == "solar"
    assert "System status" in rec["html"], "solar facts should render (System status row)"
    print("  ok: solar -> email", done["email_id"], "|", rec["subject"])


def test_misc_e2e():
    print("G3b: misc issue end-to-end...")
    sid = "char_misc"
    _run_common(sid, "Misc / other")
    chat(sid, "standard", "the fence gate was torn off")   # what_damaged (required)
    chat(sid, "standard", "high winds")                    # cause
    chat(sid, "standard", "damage_pointer_test.png")       # damage_pointer
    chat(sid, "standard", "skip")                          # additional_details
    conf = chat(sid, "standard", "skip")                   # photos -> confirm
    assert conf["state"] == "confirm", f"expected confirm, got {conf['state']}"
    done = chat(sid, "standard", "yes")
    assert done["done"] and done["email_id"], "misc flow should finish with an email"
    rec = c.get("/api/emails/" + done["email_id"]).json()
    assert rec["issue_type"] == "misc"
    assert "What is damaged" in rec["html"], "misc facts should render"
    print("  ok: misc -> email", done["email_id"], "|", rec["subject"])


# ---------------------------------------------------------------------------
# G4 / A1 — EXIF/GPS strip ACTUALLY runs for a real image (privacy invariant)
# ---------------------------------------------------------------------------
def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (24, 24), (120, 10, 10))
    exif = img.getexif()
    exif[0x0110] = "SecretCamModel"     # Model
    exif[0x013B] = "SecretArtist"       # Artist
    exif[0x9286] = "geo:27.9,-82.4"     # embedded location-ish PII
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_exif_strip_real_image():
    print("G4/A1: EXIF strip on a real JPEG...")
    raw = _jpeg_with_exif()
    # Sanity: the source really carries EXIF we care about.
    src_exif = Image.open(io.BytesIO(raw)).getexif()
    assert 0x0110 in src_exif and 0x013B in src_exif, "test fixture must carry EXIF"

    r = c.post("/api/upload", files={"file": ("photo.jpg", raw, "image/jpeg")})
    assert r.status_code == 200, f"upload should succeed, got {r.status_code}: {r.text}"
    fname = r.json()["filename"]
    saved = upload.UPLOAD_DIR / fname
    assert saved.exists(), "saved file should exist"

    out_exif = dict(Image.open(saved).getexif())
    assert 0x0110 not in out_exif, "Model EXIF tag must be stripped"
    assert 0x013B not in out_exif, "Artist EXIF tag must be stripped"
    assert 0x9286 not in out_exif, "embedded comment/location must be stripped"
    assert len(out_exif) == 0, f"no EXIF should remain, found {out_exif}"
    # Clean up the artifact we wrote.
    try:
        saved.unlink()
    except OSError:
        pass
    print("  ok: real image saved with EXIF fully removed (privacy invariant holds)")


if __name__ == "__main__":
    test_tier_mapping()
    test_safety_matrix()
    test_solar_e2e()
    test_misc_e2e()
    test_exif_strip_real_image()
    print("\nALL CHARACTERIZATION CHECKS PASSED")
