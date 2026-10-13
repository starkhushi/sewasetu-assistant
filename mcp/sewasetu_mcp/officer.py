# -*- coding: utf-8 -*-
"""
Sewa Setu for the block officer: what is waiting, one case at a time, approve or
reject with a reason, and the complaints queue. Every decision is recorded by the
portal against the officer who signed in; the assistant cannot sign as anyone else.
"""

import base64

from mcp.server.mcpserver.utilities.types import Image

from .common import Portal, PortalError, make_server, serve, problem

PATH = "/mcp/officer"
PORT = 8102

INSTRUCTIONS = """You assist a block officer of the Department of Social Welfare, Purvanchal, with the Old Age Pension queue on Sewa Setu. The officer decides; you fetch, summarise and record.

How to work:
- Start with queue_summary: how many are waiting, how many will be approved automatically within 7 days if nobody acts, how many complaints are open.
- pending_queue lists cases oldest first with days_left before deemed approval and short flags (things worth a look, not verdicts). Offer the oldest ones first.
- Before any decision, call case_details and put the facts in front of the officer: age on the day of applying, document on file or not, same mobile or bank account elsewhere, how it was filed. Show the document with view_age_proof when there is one.
- Approve or reject only when the officer says so, one case at a time. A rejection needs a reason the applicant can act on; ask for it if not given. Never invent a reason. Never decide on your own.
- Say clearly what was recorded: the status, the officer's name on it, the reason, the month the pension runs from.
- For complaints (open_complaints): show the bank account and IFSC on record so the officer can check them against the bank, then close with resolve_complaint and what was done.
- Keep answers short; the officer has a queue.
"""

mcp = make_server("Sewa Setu – officer", INSTRUCTIONS, "officer", PATH, PORT)
portal = Portal()


@mcp.tool()
async def queue_summary() -> dict:
    """Start here. Pending count by block, how many are past or near the 15-day limit
    (and will be deemed approved), how many were filed through assistants, open
    complaints, and what the officer has decided today."""
    try:
        return await portal.call("GET", "/officer/summary")
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def pending_queue(block: str | None = None, limit: int = 10, filed_via_assistant_only: bool = False) -> dict:
    """Pending applications, oldest first, with days pending, days left before deemed
    approval, and flags. block is one of Sonari, Rajapara, Dhemaji Pathar, Borgaon, Namti,
    Khelua; leave empty for all. limit up to 50."""
    params = {"limit": limit}
    if block:
        params["block"] = block
    if filed_via_assistant_only:
        params["channel"] = "AGENT"
    try:
        return await portal.call("GET", "/officer/queue", params=params)
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def case_details(application_no: str) -> dict:
    """Everything on file for one application: the form as submitted, age on the day of
    applying, whether an age-proof file is stored, flags, the full audit trail, and any
    complaints. Call before deciding."""
    try:
        return await portal.call("GET", "/officer/case/" + application_no.strip())
    except PortalError as e:
        return problem(str(e))


@mcp.tool(structured_output=False)
async def view_age_proof(application_no: str) -> Image | dict:
    """The age-proof document stored for an application, as an image when it is a JPG or
    PNG. For a PDF, says so and gives its name and size."""
    try:
        doc = await portal.call("GET", "/officer/case/%s/document" % application_no.strip())
    except PortalError as e:
        return problem(str(e))
    if doc["mime"] in ("image/jpeg", "image/png"):
        return Image(data=base64.b64decode(doc["content_base64"]), format="jpeg" if doc["mime"] == "image/jpeg" else "png")
    return {"filename": doc["filename"], "mime": doc["mime"], "bytes": doc["bytes"],
            "message": "the document is a %s of %d bytes; open it from the admin page %s"
                       % (doc["mime"], doc["bytes"], "/admin/applications")}


@mcp.tool()
async def approve(application_no: str, note: str = "") -> dict:
    """Approve a pending application in the signed-in officer's name. Only on the
    officer's say-so. An optional note goes into the audit trail."""
    try:
        return await portal.call("POST", "/officer/case/%s/decision" % application_no.strip(),
                                 json={"decision": "approve", "reason": note})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def reject(application_no: str, reason: str) -> dict:
    """Reject a pending application in the signed-in officer's name, with a reason the
    applicant can act on (at least 10 characters; it is sent to them by SMS). Only on the
    officer's say-so; never supply the reason yourself."""
    try:
        return await portal.call("POST", "/officer/case/%s/decision" % application_no.strip(),
                                 json={"decision": "reject", "reason": reason})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def search(mobile: str | None = None, name: str | None = None, application_no: str | None = None) -> dict:
    """Find applications by mobile number, by (part of) the applicant's name, or by
    application number. Any status."""
    params = {k: v for k, v in dict(mobile=mobile, name=name, application_no=application_no).items() if v}
    try:
        return await portal.call("GET", "/officer/search", params=params)
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def open_complaints(limit: int = 10) -> dict:
    """Complaints raised by citizens that no officer has closed yet, oldest first, with the
    bank account and IFSC on record so they can be checked against the bank."""
    try:
        return await portal.call("GET", "/officer/grievances", params={"status": "OPEN", "limit": limit})
    except PortalError as e:
        return problem(str(e))


@mcp.tool()
async def resolve_complaint(grievance_id: int, what_was_done: str) -> dict:
    """Close a complaint in the officer's name with what was done or found (at least 10
    characters; it is sent to the citizen by SMS)."""
    try:
        return await portal.call("POST", "/officer/grievances/%d/resolve" % grievance_id,
                                 json={"resolution": what_was_done})
    except PortalError as e:
        return problem(str(e))


if __name__ == "__main__":
    serve(mcp, PATH, PORT)
