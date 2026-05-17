# snoop

A [Claude Code](https://claude.com/claude-code) **skill** that finds and
verifies a person's work email address.

Give it a name + company (or a LinkedIn URL, or freeform text). It does a
little research to guess the company's email pattern, generates a ranked
list of candidate addresses, then verifies the whole list in a **single
batched SMTP `RCPT` probe** and reports the verified hit — or a ranked
best-guess when the domain can't be definitively verified.

It only performs SMTP `RCPT` probing. **It never sends mail.** Use it for
legitimate outreach and verification only.

## What's in here

| File | Purpose |
|---|---|
| `SKILL.md` | The skill itself — instructions Claude Code loads. |
| `verify_email.py` | Standalone verifier. Single-address or batched. |
| `requirements.txt` | One dependency: `dnspython`. |

## Install (as a Claude Code skill)

Clone it into your personal skills directory as a folder named `snoop`:

```bash
git clone https://github.com/saraswatayu/snoop.git ~/.claude/skills/snoop
pip install -r ~/.claude/skills/snoop/requirements.txt
```

Then in Claude Code just say things like:

- `snoop Jane Doe at acme.com`
- `find the email for <LinkedIn profile URL>`
- `verify jane.doe@acme.com`

## Use the verifier directly (no Claude needed)

```bash
pip install -r requirements.txt

# Single address
python3 verify_email.py "jane.doe@acme.com"

# Batched, ranked list (stops at the first verified hit)
python3 verify_email.py a@acme.com b@acme.com c@acme.dev
python3 verify_email.py --file candidates.txt        # one per line
printf 'a@acme.com\nb@acme.com\n' | python3 verify_email.py --file -
```

Single mode prints one JSON verdict and its exit code mirrors the verdict
(`0` verified, `1` invalid, `2` catch_all, `3` inconclusive, `4` bad_syntax,
`5` no_mx). Batch mode prints one JSON summary (`result`, `hit`, `tested`)
and exits `0` only if a verified hit was found.

## How verification works

Per domain it does **one** MX lookup, **one** catch-all sentinel probe
(RCPT a random non-existent localpart), and reuses **one** SMTP connection
for all candidates, stopping early on the first `verified`. Google
Workspace / Microsoft 365 and catch-all domains commonly can't be
definitively verified — those are reported honestly as best-guess, never as
"Verified".

This uses SMTP `RCPT` + catch-all detection, deliberately *not* the
unreliable and widely disabled SMTP `VRFY` command.

## Responsible use

This tool probes mail servers. Probe only addresses you have a legitimate
reason to contact, keep volume low (it is sequential by design), and respect
the target's terms of service and applicable law. It is not for bulk
scraping or spam.

## License

MIT — see [LICENSE](LICENSE).
