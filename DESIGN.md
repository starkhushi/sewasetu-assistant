# Design note

Khushi, Build for Bharat fellowship take-home, October 2026.

The Director wanted any AI assistant to be able to apply for the old age pension and follow it up,
following the same rules as the paper form. She also wanted an assistant for the block officer.

## The three decisions that mattered most

### 1. All the rules stay inside the portal

The AI assistant never writes to the database directly. It can only ask the portal, and the portal
checks every request with the same rules as the website form: age 60 or above, one application per
person, an age proof, a bank account.

Before anything is submitted, the portal shows the assistant a preview. The assistant has to read
it back to the person and get a clear "yes" to the declaration. Only then will the portal accept it.
If anything changes after the preview, the person has to see it again.

For officers, approve and reject are recorded in the name of the officer who signed in. A rejection
without a reason is refused.

Why: the Director's IT team was worried the AI would make mistakes or skip checks. This way, if the
AI makes a mistake, the portal simply says no. A wrong application can never get in.

### 2. People sign in on the portal's own page

When an assistant connects, the portal's own sign-in page opens. Citizens type their mobile number
and OTP there; officers type their username and password. The assistant never sees the OTP or the
password. It only gets a permission that lasts two hours (a working day for officers).

Citizens and officers also use two separate links. A citizen's assistant can't even see the
officer's tools.

Why: old people share phones and sit at kiosks. If the assistant held the OTP, or acted for
"whoever is typing", someone could apply in another person's name. The cost is one extra sign-in
step, which can confuse people the first time.

### 3. The portal tells the assistant what to do next

I tested with small AI models on purpose, because cheap phones and free assistants use small
models. They made mistakes a careful clerk wouldn't:

- It said "I will send you a link" but never made one.
- It once made up a fake government website link.
- It kept asking a 57-year-old for his bank details before telling him he isn't eligible.

Writing better instructions helped a little. What fixed it was the portal giving one clear next step
after every answer: "ask for the IFSC", or "stop, he is 57, he can apply from 12 August 2029", or
"give them this exact upload link" (a real link the portal makes itself).

The answers about an application are also in plain words: who decided, why, which bank account the
money goes to, and from which month the pension is paid. I added this because the most common
complaint I found online was "my pension was approved but the money never came". From the same
reading I added a complaint button for unpaid pensions, paying arrears from the month of applying,
and a reminder for the yearly life certificate.

## What I would do differently with more time

I ran each invented person through the assistant only once. AI models don't answer the same way
every time, so one pass only shows that the flow can work, not how often it works. With more time I
would run each person ten times and report a pass rate. I would also add Hindi and Assamese speakers,
because that is how most applicants will actually talk to the assistant.
