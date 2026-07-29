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
    # ── Real "RFX | Estimate" samples captured from the live inbox ─────────────
    # These emails carry the proposal link only as a tokenized tracking URL
    # (url<NNNN>.roofix.io/ls/click?upn=...) — they do NOT include the raw
    # roofix.io/project/<id> URL anywhere. Consequence: parser correctly
    # returns project_id=None. Downstream, the scraper follows the tracking
    # URL to reach the proposal and reads the project id off landed_url.
    {
        "label": "estimate_in_progress_gerald_kang_836_lasser_drive",
        "sender": '"RFX | Estimate" <no-reply@roofix.io>',
        "to": ["peyton.anderton@zeoenergy.com"],
        "subject": "Estimate in Progress - Gerald kang - 836 Lasser Drive",
        "body_text": (
            "Hello, We have received your request to provide an estimate for Gerald kang - 836 Lasser Drive "
            "The Estimate is now being prepared and we will notify you as soon as it is ready. Under normal"
        ),
        "body_html": (
            'Hello,<div></div>\r\n<br />We have received your request to provide an estimate for '
            'Gerald kang - 836 Lasser Drive<br /><br />The Estimate is now being prepared and we will '
            'notify you as soon as it is ready. Under normal circumstances, this should take 5-10 minutes '
            'to complete.<br /><br /><a href="http://url6628.roofix.io/ls/click?upn=u001.4Z6Wlupp8uqsB8wu9v'
            'HugyJ38vRluDNCZqFZOHq7UjNwbWkz16GpbT93NKggEIZq-2FPhR0n6WCaMnkgmD7icOMhxpPl50t7BNBUEwRw6-2FJuM'
            '-3DdKAy_KLSSFdUhM9sjWVJWc9jgSEyNQLXXUsooFxlm6Kgi-2BMMjHo5wLaf-2FGRLHGvnqIQXae9NdYU1UkzGVMUfIvy'
            '47U8XkYGLtTT7QJe-2B8hI77AL-2BImvjUkx1SQ8jPwAFKXYfy6OW0TP7ObsSiPvMCi9k9Uj69fyjVL5HlQ-2B-2FFYj1N'
            'C1qRbzHjSC9G3WWRFBovWF6t-2FLuTTLnJXnWJKpicJYpL8rjT-2Bme2jZoJ-2BhyBeYfLqPNfuPberxeOUUjfM-2F63F'
            '03eH3y1LC50WMKEJ66mfr4EvcITX1xxXv3QTmLU5o48Kd5w-2F2qHOrLhFhMvT2ONE6qu" target=_blank>'
            '<font color="#0000ff">View the Project here</font></a>.<br /><br />Do not reply. This '
            'email address is not monitored. Follow the link to view further details.'
        ),
        "timestamp": "2026-07-23T22:37:43+00:00",
    },
    {
        "label": "estimate_in_progress_linda_ward_1512_fairlane_avenue_southwe",
        "sender": '"RFX | Estimate" <no-reply@roofix.io>',
        "to": ["cole.fife@zeoenergy.com"],
        "subject": "Estimate in Progress - Linda ward - 1512 Fairlane Avenue Southwest",
        "body_text": (
            "Hello, We have received your request to provide an estimate for Linda ward - 1512 Fairlane "
            "Avenue Southwest The Estimate is now being prepared and we will notify you as soon as it is "
            "ready. Under"
        ),
        "body_html": (
            'Hello,<div></div>\r\n<br />We have received your request to provide an estimate for '
            'Linda ward - 1512 Fairlane Avenue Southwest<br /><br />The Estimate is now being prepared '
            'and we will notify you as soon as it is ready. Under normal circumstances, this should '
            'take 5-10 minutes to complete.<br /><br /><a href="http://url6628.roofix.io/ls/click?upn='
            'u001.4Z6Wlupp8uqsB8wu9vHugyJ38vRluDNCZqFZOHq7UjPtPt2Zh-2Bzm7Kj7LFDEs3UoQa6vnQFeuHxkWoVu1Sqm'
            'YymxU1Rx98z7hYT2-2Buw15GU-3DypYk_KLSSFdUhM9sjWVJWc9jgSEyNQLXXUsooFxlm6Kgi-2BMMjHo5wLaf-2FG'
            'RLHGvnqIQXae9NdYU1UkzGVMUfIvy47U0wf30q0Iw0sMJsQKhp0HX6U9uTk-2B7R-2FsyHwpcwrvFSdnoOTaRH-2Fc'
            'dmg6JJKVKUbO0khrao5Lt4uUh-2BgKOANKrniUSco-2ByLZmaORo1taYyQxOvnwLViBDylfleUEJmFkBPNteA30MVU'
            'vR0ev1op3DZ1fKZNwB6jXy3IEc2gFI4I5GFK6w7xgVFS4KRglP7ROttS9KMqwtYMXd1gwGmuTBavb0is7oMHL2-2FH'
            'WcWnUj0TkyWfilLBv3N1bK4DZaFQ5QA-3D-3D" target=_blank><font color="#0000ff">View the Project '
            'here</font></a>.<br /><br />Do not reply. This email address is not monitored. Follow the '
            'link to view further details.'
        ),
        "timestamp": "2026-07-23T19:59:36+00:00",
    },
    {
        "label": "estimate_in_progress_cynthia_stoneham_11706_pierce_court",
        "sender": '"RFX | Estimate" <no-reply@roofix.io>',
        "to": ["andrew.lusk@gosunergy.com"],
        "subject": "Estimate in Progress - Cynthia  Stoneham - 11706 Pierce Court",
        "body_text": (
            "Hello, We have received your request to provide an estimate for Cynthia Stoneham - 11706 "
            "Pierce Court The Estimate is now being prepared and we will notify you as soon as it is "
            "ready. Under normal"
        ),
        "body_html": (
            'Hello,<div></div>\r\n<br />We have received your request to provide an estimate for '
            'Cynthia &nbsp;Stoneham - 11706 Pierce Court<br /><br />The Estimate is now being prepared '
            'and we will notify you as soon as it is ready. Under normal circumstances, this should take '
            '5-10 minutes to complete.<br /><br /><a href="http://url6628.roofix.io/ls/click?upn=u001.4Z'
            '6Wlupp8uqsB8wu9vHugyJ38vRluDNCZqFZOHq7UjOK02kSh2t6b9wqqNbwbDEYI7QF4WLgaHk70sJVb2z7DBzFwKYm'
            'FJsIrZ1DNCnuuTQ-3DpN2K_KLSSFdUhM9sjWVJWc9jgSEyNQLXXUsooFxlm6Kgi-2BMMjHo5wLaf-2FGRLHGvnqIQX'
            'ae9NdYU1UkzGVMUfIvy47U7b03FVvNjymwqQ9eHWgr3p-2BmU0IaSm7Q7MSWL1vr-2BiYBYXXV6rBIWoCgkvA8DB4'
            'vZ0LJTc3FkoRKL4QQmMePmvSPzYnDj-2BJjWitu-2FDCb7hZ9eJD-2BfH08-2FK9TYFQTgnW1Kn-2BVLeaFWqumUm'
            '29Ommm0ZWvXQp7-2BsXuqXWx88-2FntorIo82NeQ6qNJYwuReZ0ZgLU-2BeAPr0tiVL6JhhkPXq3KRI3weZaV4bBE'
            '71BIQrJieX" target=_blank><font color="#0000ff">View the Project here</font></a>.<br /><br />'
            'Do not reply. This email address is not monitored. Follow the link to view further details.'
        ),
        "timestamp": "2026-07-24T13:57:28+00:00",
    },
]
