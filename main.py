"""LeadBot command line.

  python main.py init                    create the database
  python main.py scrape companies.csv    find emails on company websites
  python main.py discover [category] [--max N]   search the web for new leads
  python main.py import lenders|microfinance [PDF]   add every lender or microfinance bank in a CBK directory
  python main.py import names FILE --industry sacco --source "SASRA 2026" [--max N]
                                         look up a register's names (one per line) on the web
  python main.py verify                  check email domains
  python main.py personalise [--limit N] write a personal opening for verified leads from their websites
  python main.py rank                    top 20 waiting leads in sending order, with start year and score
  python main.py send --dry-run          preview emails without sending
  python main.py preview [industry] [--limit N]   show emails (plain and HTML) with their opener and use case
  python main.py send                    send today's batch
  python main.py sent [--days N]         list what was sent (date, company, email, step, subject)
  python main.py sent --show a@b.com     print the full email sent to that address
  python main.py inbox                   check replies once
  python main.py inbox --catch-up        re-read mail earlier checks skipped; matches are held for you
  python main.py watch                   check replies every few minutes (leave running)
  python main.py auto                    weekdays 9am: discover, verify, personalise, send; inbox in between
  python main.py stats                   counts by status and reply label
  python main.py unsubscribe a@b.com     manually opt someone out
  python main.py export [folder]         write leads.csv, events.csv and replies.csv (joined on lead_id)
"""
import sys

import db


def take_number(args, flag):
    """Remove `flag N` from args and return N, or None if the flag isn't there."""
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
        sys.exit(f"{flag} needs a number, e.g. {flag} 5")
    n = int(args[i + 1])
    del args[i:i + 2]
    return n


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    db.init()

    if cmd == "init":
        print("Database ready.")
    elif cmd == "scrape":
        import scraper
        scraper.run(args[0] if args else "companies.csv")
    elif cmd == "discover":
        import discover
        max_new = discover.DEFAULT_MAX
        if "--max" in args:
            i = args.index("--max")
            max_new = int(args[i + 1])
            del args[i:i + 2]
        discover.run(args[0] if args else None, max_new)
    elif cmd == "import":
        import targeting
        kind = args.pop(0) if args else ""
        if kind == "lenders":
            targeting.import_lenders(args[0] if args else targeting.CBK_DCP_URL)
        elif kind == "microfinance":
            targeting.import_microfinance(args[0] if args else targeting.CBK_MFB_URL)
        elif kind == "names":
            industry, source = targeting.take_option(args, "--industry"), targeting.take_option(args, "--source")
            limit = take_number(args, "--max")
            if not args or not industry or not source:
                sys.exit('Usage: python main.py import names FILE --industry sacco --source "SASRA 2026"')
            targeting.import_names(args[0], industry, source, limit)
        else:
            sys.exit("Usage: python main.py import lenders|microfinance [PDF]  or  import names FILE ...")
    elif cmd == "auto":
        import auto
        auto.run()
    elif cmd == "verify":
        import verify
        verify.run()
    elif cmd == "send":
        import sender
        sender.run(dry_run="--dry-run" in args)
    elif cmd == "sent":
        import sender
        if "--show" in args:
            i = args.index("--show")
            if i + 1 >= len(args):
                print("Usage: python main.py sent --show a@b.com")
                return
            sender.show_sent(args[i + 1])
        else:
            days = None
            if "--days" in args:
                i = args.index("--days")
                if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
                    print("Usage: python main.py sent --days 7")
                    return
                days = int(args[i + 1])
            sender.sent_log(days)
    elif cmd == "personalise":
        import config
        import personalise
        limit = take_number(args, "--limit")
        if not config.USE_AI_OPENER:
            print("AI openers are off (USE_AI_OPENER=false in .env): emails use the templates only.")
        elif not personalise.run(limit=limit) and not db.leads_to_personalise(1):
            print("No verified leads waiting for a personal opening.")
    elif cmd == "rank":
        import ranking
        ranking.show()
    elif cmd == "preview":
        import sender
        limit = take_number(args, "--limit") or 1
        sender.preview(args[0] if args else None, limit)
    elif cmd == "inbox":
        import inbox
        if "--catch-up" in args:
            print(f"Caught up {inbox.check_once(catch_up=True)} messages an earlier check had skipped.")
        else:
            print(f"Handled {inbox.check_once()} replies.")
    elif cmd == "watch":
        import inbox
        inbox.watch()
    elif cmd == "stats":
        leads, replies = db.stats()
        print("Leads:", leads or "none")
        print("Replies:", replies or "none")
        import config
        import sender
        emailed, bounced = db.bounce_stats(config.BOUNCE_WINDOW_DAYS)
        rate = f"{100 * bounced / emailed:.1f}%" if emailed else "n/a"
        print(f"Bounces: {bounced} of {emailed} companies emailed in the last "
              f"{config.BOUNCE_WINDOW_DAYS} days ({rate}, limit "
              f"{config.BOUNCE_LIMIT_PERCENT:g}%)" + (" - SENDING PAUSED" if sender.bounce_gate() else ""))
    elif cmd == "unsubscribe" and args:
        db.unsubscribe(args[0])
        print(f"{args[0]} unsubscribed.")
    elif cmd == "export":
        for name, n in db.export(args[0] if args else ".").items():
            print(f"{name}: {n} rows")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
