"""eTLD+1 extraction: the one definition of what `citation.domain` means.

Spec section 6, gate 3, requires promotion evidence to span at least two
*registrable* domains, "so blog.foo.com and foo.com count once". That only
holds if something actually reduces a host to its registrable domain, and
nothing did: schemas/citation.json asked for any non-empty string,
confidence.py asserted eTLD+1 in a docstring and nowhere else, and
publicsuffix2 sat in the dependency list unimported. The measured result
was confidence.compute(["blog.foo.com", "foo.com", "foo.com"], "supported")
== 0.6 — promotable, on what is really one source. Exactly the outcome
gate 3 exists to prevent.

This module is deliberately NOT wired into Memory.create. The store
validates, it does not transform: normalizing on write would make the sole
writer silently rewrite its caller's data, and a caller that handed over a
raw host would never find out. Callers normalize here, then write, and the
schema's `domain` pattern catches anyone who skipped the step.
"""
import re
from urllib.parse import urlsplit

import publicsuffix2

# Mirrors schemas/citation.json's `domain` pattern; tests assert the two
# stay identical, so this module cannot emit a value the store rejects.
HOSTNAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

_IPV4 = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")


def host_of(host_or_url):
    """The bare lowercase hostname of a URL or a hostname.

    Strips scheme, userinfo, port, path and the FQDN's trailing dot.
    Returns "" when there is no host to find.
    """
    text = (host_or_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # urlsplit only reads the first component as a host when the string
        # starts with a scheme or with '//'; otherwise it is taken as a path.
        text = "//" + text.lstrip("/")
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return ""
    return (host or "").strip(".")


def registrable(host_or_url):
    """The registrable domain (eTLD+1) of a hostname or an absolute URL.

    blog.foo.com and foo.com both collapse to foo.com; multi-part public
    suffixes are honoured, so www.bbc.co.uk gives bbc.co.uk rather than
    co.uk. Offline — publicsuffix2 ships the public suffix list.

    An IPv4 literal is returned unchanged: it has no registrable domain,
    but it is still the unit of source independence, and publicsuffix2
    would otherwise reduce 192.168.1.1 to "1" and make every RFC1918 host
    look like the same source.

    Raises ValueError on anything with no registrable domain in it, rather
    than returning a value the citation schema would reject downstream.
    """
    host = host_of(host_or_url)
    if not host:
        raise ValueError(f"no host in {host_or_url!r}")
    if _IPV4.match(host):
        return host
    domain = publicsuffix2.get_sld(host)
    if not domain or not HOSTNAME.match(domain):
        raise ValueError(f"{host_or_url!r} has no registrable domain")
    return domain
