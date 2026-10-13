# Sewa Setu, agent-ready

This is my take-home for the Build for Bharat fellowship (AI track). The Director's ask was simple
to say: a grandson on a bus should be able to ask his phone's AI assistant to apply for his
grandmother's pension, and it should just work, with the portal staying the system of record and
the assistant following the same rules as the paper form. Her block officers should get an
assistant too.

Live: https://sewasetu-eo77.onrender.com

| | |
|---|---|
| Citizen MCP endpoint | https://sewasetu-eo77.onrender.com/mcp/citizen |
| Officer MCP endpoint | https://sewasetu-eo77.onrender.com/mcp/officer |
| Chat assistant on the site (Gemma) | https://sewasetu-eo77.onrender.com/assistant |
| How to connect an assistant | https://sewasetu-eo77.onrender.com/agents |
| Eval dashboard | https://sewasetu-eo77.onrender.com/eval-dashboard |
| SMS inbox (OTPs land here, not on a phone) | https://sewasetu-eo77.onrender.com/__gateway/ |

The design note is in [DESIGN.md](DESIGN.md).

## What I built

**A JSON API inside the portal** ([app/api.py](app/api.py)). Both assistants talk to the portal
through this and nothing else. A citizen's application is built up as a draft, read back as a
preview, and only goes in through the portal's own `validate_application()` and
`create_application()`, so an assistant can't get anyone a pension the counter wouldn't. Every
officer decision goes through one `decide_application()` that the web admin pages now use as well,
so the record of who decided and why is the same whichever way it was made.

**Two MCP servers on separate endpoints** ([mcp/](mcp/)). The citizen server never lists officer
tools. Each one is an OAuth resource server that points clients at the portal's own authorization
server, so people sign in on the portal's pages (citizens by mobile and OTP, officers with the
departmental login) and the assistant never sees the OTP or password. The MCP servers hold no data
and no secrets; they forward the person's own token to the portal API, which checks it again.

Citizen tools: scheme rules, my status, save details, upload link, review, submit, status in plain
words, withdraw, report a payment problem, confirm alive. Officer tools: queue summary, pending
queue (oldest first, days left before deemed approval), case details with flags, view age proof,
approve, reject (a reason is required), search, open complaints, resolve complaint.

**Three things I added after reading about why old-age pensions actually fail.** Most complaints I
found (news reports, a CAG audit, grievance forums) were not about applying, they were about what
happens after:

- *"Approved but the money never came."* The status now says which account (last four digits) and
  IFSC the money goes to, and the citizen can raise a complaint that sits in the officer's queue
  until she closes it with a recorded reason.
- *Arrears lost to a wrong start date.* CAG found effective dates set months after the application.
  An approval now sets the pension to run from the month of application, and the status shows
  what is owed so far.
- *Pensions stopped for a missed life certificate.* Approved pensions carry a yearly due date, the
  status warns when it is close or overdue, and the citizen can confirm through the assistant.

**A chat assistant on the website** ([app/assistant.py](app/assistant.py)) for people who don't
have an assistant of their own. It is just another MCP client: it signs in through the same OTP
page and uses the same citizen tools, with Gemma 4 as the model.

**A redesign of the portal pages** for old applicants on phones: it now fits a phone screen, has
bigger text and buttons, a progress bar on the form, and a plain-words status banner. Every page
says it is a demonstration, because Chrome's Safe Browsing flagged the free Render URL as
phishing (a government-looking login page on a free subdomain).

**One bug fix in the portal I was handed.** Application numbers are random six digits, and with
100k rows already there a new one matched an existing number about one time in nine. Officers look
cases up by that number, so a decision could land on the wrong person. New numbers are now checked
and the column has a unique index.

## Evals

The people I invented are in [evals/personas.yaml](evals/personas.yaml): Parboti and her grandson
on the bus, a daughter whose mother's pension was approved but never paid, a 57-year-old who
insists he is 61, a family about to apply twice, a kiosk operator who catches his own IFSC typo,
an 81-year-old whose life certificate is overdue, a woman who switches from her son's bank account
to her own at the preview, and a block officer who tries to reject without a reason.

[evals/run.py](evals/run.py) signs each one in through the portal's real pages, has a model play
them against the live MCP endpoint, then reads the database and checks what must be true. The
assistant is Gemma 4 acting as a plain MCP client ([evals/gemma_agent.py](evals/gemma_agent.py));
`ASSISTANT=claude` runs Claude Code headless instead. [evals/smoke.py](evals/smoke.py) checks the
rules directly against the API, with no model involved, so a conversation can't pass for the wrong
reason.

## Running it

Locally:

```
cp .env.example .env          # set ADMIN_PASSWORD, and GEMINI_API_KEY for the chat
docker compose up --build
python evals/smoke.py         # 31 rule checks
python evals/run.py           # the personas (slow on Gemma's free tier)
```

Portal on :8000, citizen MCP on :8101, officer MCP on :8102.

Hosted: I didn't have a card for a VM, so the live site is a single container on Render's free plan
([deploy/render/](deploy/render/), [render.yaml](render.yaml)) with Postgres on Neon's free tier.
Caddy inside the container routes the two MCP paths and the portal. For a real VM, `docker-compose.prod.yml`
plus [deploy/setup.sh](deploy/setup.sh) does the same with automatic HTTPS.

## The portal as it was handed over

Department of Social Welfare, Government of Purvanchal (fictional).

- Status portal: `/status` (mobile number + date of birth as DDMMYYYY)
- Departmental login: `/admin` (user `admin`, password from `ADMIN_PASSWORD`)
- SMS gateway console (simulated telecom): `/__gateway/`, also as JSON at `/__gateway/api/messages?to=<mobile>`
- Database: Postgres, loaded from `seed/seed.sql` (~100k applications)
- A nightly job (`scripts/deemed_approval.py`) deems approved anything pending past the 15-day SLA.
- The portal is an OAuth 2.1 authorization server (RFC 8414 discovery, RFC 7591 registration,
  PKCE, RFC 8707 resource indicators, refresh rotation within an absolute session, revocation,
  JWKS). Citizen sessions last 2 hours, officer sessions 8.
