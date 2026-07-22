"""
Test the PARSER alone against real Roofix email samples (Contract B).
Run from ai/roofix-bridge/:   PYTHONPATH=. python tests/test_parser.py
"""

from components.parser import parse_email
from tests.roofix_email_samples import SAMPLES


EXPECTED = {
    "new_comment_with_mention": {"event_type": "New Comment",
                                 "customer_name": "LaFonda Mcwilliams Wyatt",
                                 "mentioned_users": ["Andrew_Lusk"],
                                 "parse_complete": True},
    "new_comment_thread":       {"event_type": "New Comment",
                                 "customer_name": "LaFonda Mcwilliams Wyatt",
                                 "parse_complete": True},
    "new_task_select_funding":  {"event_type": "New Task",
                                 "customer_name": "Debbie Bush",
                                 "parse_complete": True},
    "estimate_complete":        {"event_type": "Estimate Complete",
                                 "customer_name": "David Estes",
                                 "address_suffix": "Reorder",
                                 "parse_complete": False},
    "estimate_in_progress":     {"event_type": "Estimate",
                                 "customer_name": "Rosa Gonzales",
                                 "parse_complete": False},
    "hic_executed":             {"event_type": "HIC Executed",
                                 "customer_name": "Conner broaddus",
                                 "parse_complete": True},
    "install_date_confirmed":   {"event_type": "Install Date",
                                 "customer_name": "Robert Shepherd",
                                 "parse_complete": True},
    "new_task_with_url_in_body":{"event_type": "New Task",
                                 "project_id": "1780583972085x1910864934000000000",
                                 "parse_complete": True},
}


def run():
    passed = failed = 0
    for s in SAMPLES:
        ev = parse_email(s).as_dict()
        exp = EXPECTED.get(s["label"], {})
        problems = []
        for k, want in exp.items():
            got = ev.get(k)
            if got != want:
                problems.append(f"{k}: expected {want!r}, got {got!r}")
        if problems:
            failed += 1
            print(f"FAIL  {s['label']}")
            for p in problems:
                print(f"        {p}")
        else:
            passed += 1
            print(f"ok    {s['label']:26s} -> {ev['event_type']}, "
                  f"{ev['customer_name']}, complete={ev['parse_complete']}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
