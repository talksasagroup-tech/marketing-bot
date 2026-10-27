"""Checks that each email's domain can receive mail (MX record lookup).
This catches typos and dead domains. It can't confirm the mailbox itself exists."""
import re
import dns.exception
import dns.resolver

import db

SYNTAX = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
# The answers that really mean "this domain can't receive mail". Anything else (a timeout,
# no network, a failing name server) says nothing about the domain, so the lead is left to
# be checked again.
NO_SUCH_DOMAIN = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer)
_cache = {}


def _has(domain, record):
    """True if the domain has this record, False if DNS says it hasn't, None if DNS didn't answer."""
    try:
        return len(dns.resolver.resolve(domain, record, lifetime=8)) > 0
    except NO_SUCH_DOMAIN:
        return False
    except (dns.exception.DNSException, OSError):
        return None


def domain_ok(domain):
    """True if the domain can receive mail, False if it can't, None if DNS couldn't tell
    (a timeout or no network): that answer isn't remembered, so the next check asks again."""
    if domain in _cache:
        return _cache[domain]
    ok = _has(domain, "MX")
    if not ok:  # some domains receive mail on their A record
        a = _has(domain, "A")
        ok = True if a else None if None in (ok, a) else False
    if ok is not None:
        _cache[domain] = ok
    return ok


def run():
    leads = db.leads_by_status("new")
    good = bad = unsure = 0
    for lead in leads:
        email = lead["email"]
        ok = domain_ok(email.split("@")[1]) if SYNTAX.match(email) else False
        if ok:
            db.set_status(lead["id"], "verified", "verify")
            good += 1
        elif ok is None:
            unsure += 1
            print(f"  couldn't check {email} (DNS didn't answer); will try again next time")
        else:
            db.set_status(lead["id"], "invalid", "verify")
            bad += 1
            print(f"  invalid: {email}")
    print(f"Verified {good}, rejected {bad}" + (f", {unsure} left to check again." if unsure else "."))
