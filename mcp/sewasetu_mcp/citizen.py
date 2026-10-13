# -*- coding: utf-8 -*-
"""
Sewa Setu for citizens: the tools an assistant gets after a citizen signs in with
mobile and OTP. Thin wrappers over the portal's API; the rules live in the portal.

Everything here is written for a small model talking to an old person, or to the
grandson, daughter or kiosk operator sitting with them: one thing at a time, read
back before submitting, never guess a date of birth.
"""

import base64

from .common import Portal, PortalError, make_server, serve, ok, problem

PATH = "/mcp/citizen"
PORT = 8101

INSTRUCTIONS = """You help a citizen of Purvanchal apply for the Old Age Pension (Rs 250 a month, age 60 and above) on the Sewa Setu portal, and follow it up. The person may be the applicant, or a grandson, daughter or kiosk operator sitting with them. They may be on a shared phone. Assume no familiarity with forms.

How to work:
- Start with my_status. It says whether there is already an application, a half-filled draft, and what to do next.
- Ask ONE question at a time, in short plain sentences. Never ask for Aadhaar; it is not needed.
- Save each answer with save_details as soon as you have it, so nothing is lost if the phone is put down.
- Date of birth: ask for it as written on the age-proof document, in DD/MM/YYYY. If they only know the year or an approximate age, say the document's date is needed and ask them to read it out. Never fill in a guess.
- Name: exactly as on the age-proof document, spelling included. Read it back letter by letter if unsure.
- The late husband's name is optional. Ask once if she is widowed; if they do not give it, move on without it.
- If one message answers several questions (e.g. "female, widow" or "Lakhipur village, Sonari block"), save all of them and ask the next missing thing.
- Bank account must be in the applicant's own name. Read the account number and IFSC back in groups (e.g. 3124 5678 901, SBIN 0003 077) so they can be checked.
- Age proof: birth certificate, school leaving certificate or Voter ID. Use upload_link so the photo can be chosen on the phone; attach_age_proof only if you actually hold the file's bytes.
- Before submitting: call review_application, read EVERY line of the preview back, read the declaration, and submit only when they say yes to both. If they change anything, review again.
- After submitting, tell them the application number, that an SMS has come, and that the officer has 15 days, after which it is approved automatically.
- If an application is already approved and "the money never came": use application_status, then report_payment_problem. Tell them which account (last 4 digits) and IFSC the money goes to, and that the officer must now look into it.
- If the portal says no (under 60, window closed, already applied), say so plainly and say what can be done instead. Do not look for a way around it.
- Do not promise an outcome. The officer decides.
"""

mcp = make_server("Sewa Setu – citizen", INSTRUCTIONS, "citizen", PATH, PORT)
portal = Portal()


@mcp.tool()
async def scheme_rules() -> dict:
    """What the Old Age Pension is, who is eligible, what is needed to apply, and how the
    15-day decision rule works. Call this to answer questions about the scheme instead of
    relying on memory."""
    try:
        return await portal.call("GET", "/scheme")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def my_status() -> dict:
    """Start here. Who is signed in (by mobile), whether they already have a live
    application (with its status in plain words), whether a draft is in progress, and
    what to do next."""
    try:
        return await portal.call("GET", "/citizen/me")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def save_details(applicant_name: str | None = None, dob: str | None = None, gender: str | None = None,
                       marital_status: str | None = None, husband_name: str | None = None,
                       village: str | None = None, block: str | None = None,
                       bank_account: str | None = None, ifsc: str | None = None) -> dict:
    """Save one or more answers to the application draft. Call it after each answer; only
    the fields you pass are changed. dob is DD/MM/YYYY as written on the age proof. gender
    is Male, Female or Other. marital_status is Married, Unmarried or Widowed; husband_name
    only if widowed, and optional. block is one of Sonari, Rajapara, Dhemaji Pathar,
    Borgaon, Namti, Khelua. Returns the draft, what is still missing, and any problems."""
    fields = {k: v for k, v in dict(applicant_name=applicant_name, dob=dob, gender=gender,
                                    marital_status=marital_status, husband_name=husband_name,
                                    village=village, block=block, bank_account=bank_account,
                                    ifsc=ifsc).items() if v is not None}
    if not fields:
        return problem("nothing to save; pass at least one field")
    try:
        return await portal.call("PUT", "/citizen/draft", json=fields)
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def show_draft() -> dict:
    """The draft as saved so far, what is still missing, and any problems."""
    try:
        return await portal.call("GET", "/citizen/draft")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def clear_draft() -> dict:
    """Throw away the draft and start again. Nothing submitted is affected."""
    try:
        return await portal.call("DELETE", "/citizen/draft")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def upload_link() -> dict:
    """A one-time web link (valid 30 minutes) to attach the age-proof photo or PDF from the
    phone's browser. Give the link to the person, wait for them to say it is done, then
    call show_draft to confirm the document is attached."""
    try:
        return await portal.call("POST", "/citizen/draft/upload-link")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def attach_age_proof(filename: str, content_base64: str) -> dict:
    """Attach the age-proof document directly, if you hold the file: filename with its
    extension (jpg, png or pdf) and the file's bytes as base64, up to 5 MB. Otherwise use
    upload_link."""
    try:
        base64.b64decode(content_base64, validate=True)
    except Exception:
        return problem("content_base64 is not valid base64")
    try:
        return await portal.call("POST", "/citizen/draft/document",
                                 json={"filename": filename, "content_base64": content_base64})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def review_application() -> dict:
    """The preview before submitting: every field as it will go in, the problems if any,
    the declaration text, and a review_token. Read the whole preview and the declaration
    back to the person. Submit only after they agree to both."""
    try:
        return await portal.call("POST", "/citizen/draft/review")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def submit_application(review_token: str, declaration_accepted: bool) -> dict:
    """Submit the reviewed draft. review_token comes from review_application and is only
    valid while nothing has changed since; declaration_accepted must be true, meaning the
    person heard the declaration and agreed. Returns the application number."""
    if not declaration_accepted:
        return problem("the person must agree to the declaration before submitting")
    try:
        return await portal.call("POST", "/citizen/draft/submit",
                                 json={"review_token": review_token, "declaration_accepted": True})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def application_status(application_no: str | None = None) -> dict:
    """Where an application stands, in plain words: received when, who decided and why,
    the deemed-approval date if still pending, the bank account (last 4 digits) and IFSC
    the pension goes to, the month it runs from and the amount payable so far, the yearly
    life-certificate date, and any open complaint. Without application_no, all of this
    mobile's applications."""
    try:
        if application_no:
            return await portal.call("GET", "/citizen/applications/" + application_no.strip())
        return await portal.call("GET", "/citizen/applications")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def withdraw_application(application_no: str, confirmed: bool) -> dict:
    """Withdraw a PENDING application so a fresh one can be made, for example because a
    detail was wrong. Ask the person first; confirmed must be true. Cannot be undone."""
    if not confirmed:
        return problem("ask the person to confirm the withdrawal first")
    try:
        return await portal.call("POST", "/citizen/applications/%s/withdraw" % application_no.strip())
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def report_payment_problem(application_no: str, what_happened: str,
                                 kind: str = "PAYMENT_NOT_RECEIVED") -> dict:
    """Raise a complaint on an approved application so the block officer must look into
    it: the pension has not come (PAYMENT_NOT_RECEIVED), it goes to the wrong account
    (WRONG_BANK_ACCOUNT), a recorded detail is wrong (DETAILS_WRONG), or OTHER. Say in a
    sentence what the person told you. Returns the complaint number and the account the
    money is being sent to."""
    try:
        return await portal.call("POST", "/citizen/applications/%s/grievance" % application_no.strip(),
                                 json={"kind": kind.upper(), "detail": what_happened})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def confirm_alive(application_no: str) -> dict:
    """Record the yearly life certificate for an approved pension. The person has just
    signed in with an OTP on their own mobile, which the portal accepts as the proof.
    Use when application_status says the confirmation is due or overdue, or when asked."""
    try:
        return await portal.call("POST", "/citizen/applications/%s/life-certificate" % application_no.strip())
    except PortalError as e:
        return problem(str(e))


if __name__ == "__main__":
    serve(mcp, PATH, PORT)
