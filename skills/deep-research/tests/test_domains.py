"""Gate 3 counts distinct citation.domain values to measure source
independence (spec section 6: ">= 3 citations across >= 2 registrable
domains (eTLD+1 via publicsuffix2, so blog.foo.com and foo.com count
once)"). Before this module nothing reduced a host to its eTLD+1 and
nothing rejected a value that was not one, so

    confidence.compute(["blog.foo.com", "foo.com", "foo.com"], "supported")

returned 0.6 — promotable, on what is really a single source.

No network: publicsuffix2 ships the public suffix list as package data.
"""
import json
from pathlib import Path

import jsonschema
import pytest

import confidence
import domains

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "citation.json").read_text()
)


# --- registrable ---------------------------------------------------------

def test_a_subdomain_and_its_parent_reduce_to_the_same_domain():
    assert domains.registrable("blog.foo.com") == "foo.com"
    assert domains.registrable("foo.com") == "foo.com"
    assert domains.registrable("blog.foo.com") == domains.registrable("foo.com")


def test_deeply_nested_subdomains_reduce_to_the_registrable_domain():
    assert domains.registrable("a.b.c.foo.com") == "foo.com"


@pytest.mark.parametrize("host,expected", [
    ("bbc.co.uk", "bbc.co.uk"),
    ("www.bbc.co.uk", "bbc.co.uk"),
    ("news.a.b.bbc.co.uk", "bbc.co.uk"),
    ("example.pvt.k12.ma.us", "example.pvt.k12.ma.us"),
])
def test_a_multi_part_public_suffix_is_not_mistaken_for_the_domain(host, expected):
    """co.uk is a public suffix, so naive last-two-labels logic would call
    two unrelated British sites the same source."""
    assert domains.registrable(host) == expected


@pytest.mark.parametrize("value", [
    "https://blog.foo.com/some/path?q=1#frag",
    "http://blog.foo.com",
    "https://blog.foo.com:8443/x",
    "https://user:pw@blog.foo.com/x",
    "blog.foo.com:8443",
    "blog.foo.com/x",
    "BLOG.FOO.COM",
    "  blog.foo.com  ",
    "blog.foo.com.",
])
def test_a_full_url_and_its_noisy_variants_all_reduce_alike(value):
    assert domains.registrable(value) == "foo.com"


def test_an_ipv4_literal_is_returned_unchanged():
    """publicsuffix2 alone reduces 192.168.1.1 to "1", which would make
    every unrelated numeric host look like one source."""
    assert domains.registrable("https://192.168.1.1/x") == "192.168.1.1"
    assert domains.registrable("10.0.0.7") != domains.registrable("10.0.0.8")


@pytest.mark.parametrize("value", [
    "", "   ", None, "not a domain!!", "localhost", "https://", "/just/a/path",
])
def test_input_with_no_registrable_domain_raises(value):
    with pytest.raises(ValueError):
        domains.registrable(value)


def test_host_of_strips_everything_that_is_not_the_host():
    assert domains.host_of("https://user:pw@Blog.Foo.COM.:8443/p?q=1") == "blog.foo.com"
    assert domains.host_of("") == ""


# --- the schema and the module must agree --------------------------------

def test_the_schema_documents_what_domain_means():
    described = SCHEMA["properties"]["domain"]["description"]
    assert "eTLD+1" in described
    assert "registrable" in described


def test_the_schema_pattern_and_the_module_pattern_are_identical():
    """Two copies of one rule; this is what stops them drifting apart and
    letting registrable() emit a value Memory.create would reject."""
    assert SCHEMA["properties"]["domain"]["pattern"] == domains.HOSTNAME.pattern


@pytest.mark.parametrize("value", [
    "not a domain!!", "", "http://foo.com", "foo com", "foo_bar.com",
    "-foo.com", "foo-.com", "foo.com/x", "foo.com:8443", "FOO.COM", "localhost",
])
def test_the_schema_rejects_a_domain_that_is_not_a_bare_hostname(value):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, SCHEMA["properties"]["domain"])


@pytest.mark.parametrize("value", ["foo.com", "bbc.co.uk", "a-b.example.org",
                                   "192.168.1.1", "xn--80ak6aa92e.com"])
def test_the_schema_accepts_a_registrable_domain(value):
    jsonschema.validate(value, SCHEMA["properties"]["domain"])


@pytest.mark.parametrize("value", [
    "https://blog.foo.com/x", "www.bbc.co.uk", "A.B.Foo.COM", "192.168.1.1",
])
def test_everything_registrable_returns_satisfies_the_schema(value):
    jsonschema.validate(domains.registrable(value), SCHEMA["properties"]["domain"])


# --- the reason any of this exists ---------------------------------------

def test_reducing_hosts_first_is_what_makes_gate_three_bite():
    """The regression in one assertion: scored raw, one source clears the
    0.67 promotion threshold; scored as eTLD+1, it does not.

    Hand-computed: raw looks like 2 distinct domains, so
    min(1, 3/3) * 2/(2+1) = 0.67; reduced is 1 domain, so 1.0 * 1/2 =
    0.5."""
    raw = ["blog.foo.com", "foo.com", "foo.com"]
    assert confidence.compute(raw, "supported") == 0.67

    reduced = [domains.registrable(h) for h in raw]
    assert confidence.compute(reduced, "supported") < 0.67


def test_two_genuinely_independent_sources_still_clear_the_threshold():
    hosts = ["blog.foo.com", "foo.com", "docs.bar.co.uk"]
    reduced = [domains.registrable(h) for h in hosts]
    assert set(reduced) == {"foo.com", "bar.co.uk"}
    assert confidence.compute(reduced, "supported") == 0.67
