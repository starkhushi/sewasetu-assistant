# Design note

Khushi, Build for Bharat fellowship take-home, October 2026.

The job, as I heard it from the Director: any AI assistant should be able to apply for the old age
pension and follow it up, through an official channel, without ever getting someone a pension the
counter wouldn't. The officer should get an assistant that helps her decide, not one that decides.

## Three decisions that mattered most

**1. The rules live in one place, and it's the portal.** The MCP servers hold no data and know no
rules. They forward the person's own token to a small JSON API inside the portal, and that API
goes through the same `validate_application()` and `create_application()` the web form uses. An
assistant can't submit in one go either: it saves a draft, asks for a review (which returns the
preview, the declaration and a review token), and submit only works with that token and an
explicit yes to the declaration. If anything changes after the review, the token stops working.
On the officer side, the web pages and the assistant both decide through one `decide_application()`,
which refuses a rejection without a reason and records the officer's name from the sign-in, not
from anything the assistant says. This is what lets me tell the IT team that a hallucinating model
can only produce a refused request, not a wrong record.

**2. People sign in on the portal, and each assistant gets a token for one server.** The portal
already had an OAuth server, so I made each MCP server a resource server that points clients at it.
The citizen types their OTP and the officer their password on the portal's own pages; the assistant
only ever holds a token whose audience is that one server, for two hours (a working day for the
officer). The citizen endpoint doesn't even list officer tools. The cost is a sign-in step the
grandson has to get through, which confused me too when I first tested it, but the alternative is
an assistant that holds an OTP or acts for "whoever is typing", and on shared phones and kiosks
that's how pensions get applied for in the wrong name.

**3. The portal tells the assistant what to do next, in plain words.** I tested with small models
on purpose, because that's what most people's phones will run. Gemma kept doing things a careful
clerk wouldn't: it promised an upload link it never made, once invented
`sewa-setu.gov.in/upload/temp_link_12345`, and walked a 57-year-old all the way to the document step
before anything stopped him. Better instructions helped a little. What fixed it was the server
answering every saved field with one concrete next step: ask for the IFSC; or "stop, they're 57,
they can apply from 12 August 2029"; or "only the document is missing, give them this exact link",
with a real link the portal made itself. Status answers work the same way: they say who decided,
why, which account ending and IFSC the money goes to, and the month the pension runs from, because
"approved but the money never came" was the most common complaint I found when I read about how
these pensions actually fail. That reading is also where the payment complaint, the arrears date
and the yearly life certificate came from.

## What I would do differently

I'd run each persona many times instead of once, and in the languages people actually speak. A
small model at temperature 0.3 passes a conversation on one run and fails it on the next; one run
per person tells me the flow can work, not how often it does. With more time I'd run every persona
ten times and report a pass rate, add a Hindi-speaking and an Assamese-speaking version of each, and
run the same set through Claude Code, the client the assessors use, which I couldn't do here because
I didn't have a Claude login on this machine.
