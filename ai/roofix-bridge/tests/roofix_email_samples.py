"""
Real Roofix email samples observed during design (subjects + bodies as seen in
the inbox). Used to test the parser in isolation. These are genuine shapes the
parser must handle — not invented examples.
"""

SAMPLES = [
    {
        "label": "new_comment_with_mention",
        "sender": "RFX | New Comment <no-reply@roofix.io>",
        "to": ["andrew.lusk@zeoenergy.com"],
        "subject": "New Project Mention - LaFonda Mcwilliams Wyatt - 1521 Farley Terrace",
        "body_text": (
            "Hello!\n"
            "You were mentioned in a new Comment: LaFonda  Mcwilliams Wyatt - 1521 Farley Terrace.\n"
            "\"I heard back from the rep at Foundation, they said it is because they are "
            "waiting for the co applicant to sign. She said she resent the reminder so the "
            "HO should receive it. @Andrew_Lusk\" - Alexas Alvarado, RFX\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-24T09:34:00",
    },
    {
        "label": "new_comment_thread",
        "sender": "RFX | New Comment <no-reply@roofix.io>",
        "to": ["andrew.lusk@zeoenergy.com"],
        "subject": "New Project Comment - LaFonda Mcwilliams Wyatt - 1521 Farley Terrace",
        "body_text": (
            "Hello!\n"
            "You have a new Comment: LaFonda  Mcwilliams Wyatt - 1521 Farley Terrace.\n"
            "\"Hi Andrew, the HIC will need signed. You can send this to the homeowner from "
            "the Send Invite button on the HIC tab in the left side project menu. Once signed, "
            "you can then submit the Approve button pop up from the Pricing tab. This will move "
            "the project to our production pipeline. Thanks!\" - Abigail Hartung, RFX\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-23T07:29:00",
    },
    {
        "label": "new_task_select_funding",
        "sender": "RFX | New Task <no-reply@roofix.io>",
        "to": ["cole.fife@zeoenergy.com"],
        "subject": "Select Funding",
        "body_text": (
            "Hello!\n"
            "Please select the Funding Type for this project.\n"
            "Click on this link to view more details:\n"
            "Debbie  Bush - 5960 Navarre Road Southwest\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-23T12:23:00",
    },
    {
        "label": "estimate_complete",
        "sender": "RFX | Estimate Complete <no-reply@roofix.io>",
        "to": ["jameson.bennett@zeoenergy.com"],
        "subject": "Estimate Complete - David Estes - 1636 Cliffwood Drive (Reorder)",
        "body_text": (
            "Hello Jameson,\n"
            "Your estimate is complete for David Estes - 1636 Cliffwood Drive (Reorder).\n"
            "Open this link to view the Proposal\n"
            "Do not reply. This email address is not monitored. Follow the links to view further details."
        ),
        "timestamp": "2026-06-23T14:26:00",
    },
    {
        "label": "estimate_in_progress",
        "sender": "RFX | Estimate <no-reply@roofix.io>",
        "to": ["charles.ellis@zeoenergy.com"],
        "subject": "Estimate in Progress - Rosa Gonzales - 1114 Greenhurst Avenue Northwest",
        "body_text": (
            "Hello,\n"
            "We have received your request to provide an estimate for Rosa Gonzales - "
            "1114 Greenhurst Avenue Northwest\n"
            "The Estimate is now being prepared and we will notify you as soon as it is ready. "
            "Under normal circumstances, this should take 5-10 minutes to complete.\n"
            "View the Project here.\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-23T15:59:00",
    },
    {
        "label": "hic_executed",
        "sender": "RFX | HIC Executed <no-reply@roofix.io>",
        "to": ["andrew.lusk@zeoenergy.com"],
        "subject": "HIC Executed - Conner broaddus - 720 Carson Drive",
        "body_text": (
            "Hello,\n"
            "The HIC for Conner broaddus - 720 Carson Drive has been executed.\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-24T08:00:00",
    },
    {
        "label": "install_date_confirmed",
        "sender": "RFX | Install Date <no-reply@roofix.io>",
        "to": ["andrew.lusk@zeoenergy.com"],
        "subject": "Install Date Confirmed - Robert Shepherd - 324 Whitely Street",
        "body_text": (
            "Hello,\n"
            "The homeowner has confirmed the install date of 7/01/26 with our team. "
            "Click here to view.\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-24T11:00:00",
    },
    {
        "label": "new_task_with_url_in_body",
        "sender": "RFX | New Task <no-reply@roofix.io>",
        "to": ["andrew.lusk@zeoenergy.com"],
        "subject": "New Project Task - Re-Approval Required",
        "body_text": (
            "Hello!\n"
            "You have a new Task for [url=https://roofix.io/project/"
            "1780583972085x1910864934000000000?tab=estimatedoc]Robert sheperd - 324 Whitely Street[/url]\n"
            "Do not reply. This email address is not monitored. Follow the link to view further details."
        ),
        "timestamp": "2026-06-24T10:00:00",
    },
]
