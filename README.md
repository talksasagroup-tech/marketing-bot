# LeadBot — bulk SMS outreach

Finds company emails, sends intro emails in small daily batches, reads replies, labels them with AI, and pings you on Telegram when someone is interested.

```
companies.csv → scrape → verify → send → inbox (AI label) → Telegram alert
```

## Setup (30 min)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill it in
python main.py init
```

**AI option A — Ollama (free, open source, local):** install from ollama.com, then `ollama pull qwen2.5:7b`. Set `AI_PROVIDER=ollama`.
**AI option B — Groq (free tier, no big PC needed):** get a key at console.groq.com. Set `AI_PROVIDER=groq` and `GROQ_API_KEY`.

**Telegram:** see the steps at the top of `notify.py`, then test with `python notify.py`.

**Test the AI:** `python classifier.py` runs 5 sample replies (including Swahili) and prints the labels.

## Daily use

```bash
python main.py scrape companies.csv   # add leads from your own list
python main.py discover sacco --max 5 # or find leads by web search (see discover.py); approve them in the CRM, To review
python main.py import microfinance    # every bank in CBK's microfinance directory (also: import lenders)
python main.py import names sasra.txt --industry sacco --source "SASRA 2026"   # look up a register's names
python main.py verify                 # drop dead domains
python main.py send --dry-run         # read 3 sample emails first
python main.py preview                # one email, plain and HTML versions
python main.py send                   # send today's batch (DAILY_LIMIT, at most 10 unless DAILY_LIMIT_OVERRIDE=true)
python main.py watch                  # leave running; alerts you on replies
python main.py stats
python main.py export                 # leads.csv, events.csv, replies.csv (join on lead_id)
python main.py auto                   # or leave this running: does all of the above on weekdays at 9am
streamlit run dashboard.py            # optional dashboard
```

To run unattended on Linux, add to crontab:
```
0 9 * * 1-5  cd /path/to/leadbot && venv/bin/python main.py send
*/5 * * * *  cd /path/to/leadbot && venv/bin/python main.py inbox
```

## Lead quality and the sales routine

- **Leads found by web search wait for review.** `discover` only guesses an organisation's industry from the search that found it, so each new lead waits in the CRM's **To review** list (admins) until someone picks its industry and approves it, or marks it Not a fit. CSV and directory imports don't need review. With no category, `discover` searches only industries approved in `uses.yaml`.
- **What's working** shows each web search's results (found, invalid, not a fit, bounced, replied), so weak searches can be removed from `discover.QUERIES`, and why lost leads were lost.
- **In each lead:** a next step with a due date (due and overdue ones rise to the top of the list), a reason whenever a lead is marked lost, and a label picker on each reply to correct the AI. The change is kept in `reply_labels`. Opt-outs and bounces can't be relabelled.
- **Health strip (admins):** inbox freshness and errors, the bounce pause, today's sends against the limit, unfinished sends, follow-ups due and leads to review. Three failed inbox checks in a row also send one Telegram alert.
- **Call list:** the CRM's **Calls** view lists organisations that got every email, never wrote back and have a phone number (scraped from their site, from the directory, or from Google Maps). Log each call: Interested makes it an open lead, Not interested closes it. A call never opts anyone out of email.
- **Answer fast:** an interested reply or a question still unanswered after `STALE_REPLY_HOURS` (2) gets a second Telegram alert on weekdays 8:00-19:00, and shows "Answer now" in the CRM. **What's working** shows how fast the team answers, results by template and subject line, and how the calls went.
- **Safer by default:** a 4xx refusal from a mail server is retried rather than counted as a bounce, a DNS timeout leaves a lead to be checked again instead of marking it invalid, an opt-out from a company address stops every address on that company's domain, a lead whose email fails 3 times stops being tried, "today" is Nairobi's date, and `auto` catches up a missed 9am run later the same day.

## Templates

`templates/<industry>.txt` is used when a lead's `industry` matches (e.g. `sacco`, `school`). Otherwise `default.txt`. The subject is the template's own fixed `Subject:` line. Two or three `Subject:` lines test subjects against each other: each lead always gets the same one, and **What's working** shows which brings more replies. Microfinance, insurance, health, logistics, ecommerce and real estate have their emails written but aren't approved yet: read them, then set `approved: true` in `uses.yaml`. Only industries marked `approved: true` in `uses.yaml` are emailed; leads in other industries are skipped and stay waiting. With `USE_AI_OPENER=false` in `.env` (the default) emails are templates only: no opener, and `{uses}` is the first three uses for the industry in `uses.yaml`. With `true`, the body has two AI slots: `{opener}` (one sentence, at most 20 words, saying what the lead does in our own words; left out when there is no good one, so the email starts with "I'm Florence from TalkSasa Communications in Nairobi.") and `{uses}` (picked from that industry's approved list in `uses.yaml`, never invented). Other placeholders: `{company} {sender} {our_company} {phone} {website} {profile_line} {attachment_line}` (`{profile_line}` is "Company profile: <PROFILE_URL>", or nothing if `PROFILE_URL` is empty).

## Getting leads (companies.csv)

Columns: `company,website,industry,email` and optionally `phone`. Leave `email` blank to have it scraped. Good public sources: SASRA licensed SACCO list, Ministry of Education school lists, Kenya Yellow Pages, chamber of commerce and trade association member lists. Skip LinkedIn scraping and scraping Google Maps pages (against their terms); the Google Places API is the allowed way, and `discover` uses it when `GOOGLE_PLACES_API_KEY` is set (held to `PLACES_MONTHLY_CAP` calls a month, because Google bills beyond its free allowance).

**Registers that list names only** (SASRA's licensed SACCOs and IRA's insurers are published in the Kenya Gazette): copy the names into a text file, one per line, and run `python main.py import names FILE --industry sacco --source "SASRA 2026"`. Each name is looked up on the web; a site counts only if its title or address has a distinctive word from the name, and the leads wait in **To review**.

## More than one sending mailbox

One mailbox can only send so much before mail providers start treating it as spam. To send more, add mailboxes on a **separate domain** (e.g. `talksasa-mail.co.ke`, so the main domain's reputation is never at risk), each with SPF, DKIM and DMARC set up, and add them to `.env` as `MAILBOX_2_EMAIL`, `MAILBOX_2_SMTP_PASS`, `MAILBOX_2_START_DATE` and so on (see `.env.example`).

- A new mailbox warms up: 5 a day in its first week, 5 more each week, up to `MAILBOX_2_MAX_PER_DAY` (30).
- First emails are shared out between the mailboxes. A follow-up always goes from the mailbox its first email went from, so it stays in the same conversation.
- Each mailbox has its own bounce check on top of the overall one, and a mailbox whose login is refused is paused for that run while the others carry on.
- The inbox check reads every mailbox, so replies, bounces and unsubscribes sent to any of them are handled. A mailbox that can't be read sends one Telegram alert a day.
- **All mailboxes together still send at most `DAILY_LIMIT`** (10 unless `DAILY_LIMIT_OVERRIDE=true`). Extra mailboxes only add room once you raise that on purpose.

## Before the first real send

- [ ] Separate outreach domain (not talksasa.co.ke), so spam flags can't hurt the main domain
- [ ] SPF, DKIM and DMARC records set on that domain (check at mail-tester.com, aim for 9/10+)
- [ ] Send a few test emails to your own Gmail and check they land in Inbox, not Spam
- [ ] Start at `DAILY_LIMIT=10` (the most the bot sends unless `DAILY_LIMIT_OVERRIDE=true`, which is announced on Telegram every run); raise it only if bounces stay under 3%
- [ ] Confirm with your boss whether TalkSasa needs ODPC registration for this

## What each label does

| Label | Lead status | Alert you? |
|---|---|---|
| interested | replied | 🔥 yes |
| question | replied | ❓ yes |
| needs_review | replied | no (stays unread in inbox) |
| not_interested | replied | no (stays unread in inbox) |
| out_of_office | replied | no |
| unsubscribe | unsubscribed, never emailed again | no |
| bounce | bounced | no |
