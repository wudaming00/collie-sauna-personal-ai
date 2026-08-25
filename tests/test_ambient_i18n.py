"""The ambient page's translation tables, checked the way they actually break.

`tOps()` is called with a VARIABLE in several places — the music button labels come from a lookup
table, the gallery's positions from an array — so any tool that greps for `tOps("literal")` cannot
see those keys. One did, during a tidy-up of the tables, and removed all ten of them: nothing threw,
no test failed, the controls simply reverted to English in a language nobody was reading at the
time. These assertions are what that lesson turned into.

They also catch the other silent one: a duplicate key in a JS object literal is resolved by keeping
the LAST, so a duplicate is not dead weight — it is a translation that looks present and is not the
one in effect.
"""
import os
import re

import pytest

AMB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "harness", "webui", "ambient.html")

# Strings the page can only reach through a variable, so no literal search will find them.
REACHED_BY_VARIABLE = [
    "Previous", "Previous track", "Play / Pause", "Play or pause", "Next", "Next track",
    "Top left", "Top right", "Bottom left", "Bottom right", "Centre",
]
TABLES = ("OPS_ZH", "OPS_ZHTW")


@pytest.fixture(scope="module")
def page():
    with open(AMB, encoding="utf-8") as fh:
        return fh.read()


def pairs(text, table):
    """Every (key, value) the table declares, across its literal and every Object.assign."""
    out = []
    for pat in (re.escape("var " + table + "={") + r"(.*?)\};",
                re.escape("Object.assign(" + table + ",{") + r"(.*?)\}\);"):
        for m in re.finditer(pat, text, re.S):
            out += re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', m.group(1))
    return out


@pytest.mark.parametrize("table", TABLES)
def test_the_tables_exist_and_are_substantial(page, table):
    assert len(pairs(page, table)) > 80


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("key", REACHED_BY_VARIABLE)
def test_strings_reached_only_through_a_variable_are_present(page, table, key):
    assert key in {k for k, _ in pairs(page, table)}, (
        "%r is passed to tOps() as a variable, so nothing that greps for tOps(\"…\") can see it" % key)


@pytest.mark.parametrize("table", TABLES)
def test_no_duplicate_keys(page, table):
    keys = [k for k, _ in pairs(page, table)]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, "a JS object literal keeps the LAST of these, so the others never apply: %s" % dupes


@pytest.mark.parametrize("table", TABLES)
def test_every_literal_the_page_translates_has_an_entry(page, table):
    # the boundary matters: without it this also matches getOps("/api/runs"), because `tOps`
    # is a suffix of `getOps` — the check then reports API paths as untranslated strings
    asked = {k.replace('\\"', '"')
             for k in re.findall(r'(?<![A-Za-z])tOps\("((?:[^"\\]|\\.)*)"\)', page)}
    for attr in ("data-ops-t", "data-ops-aria", "data-ops-title"):
        asked |= set(re.findall(attr + r'="([^"]*)"', page))
    have = {k for k, _ in pairs(page, table)}
    missing = sorted(k for k in asked - have if k.strip())
    assert not missing, "untranslated: %s" % missing[:12]


@pytest.mark.parametrize("table", TABLES)
def test_placeholders_survive_translation(page, table):
    """A translated string that drops its %n renders the sentence without its number."""
    for k, v in pairs(page, table):
        for token in ("%n", "%a", "%b", "%d", "%r"):
            if token in k:
                assert token in v, "%r loses %s in %s" % (k, token, table)


def test_explicit_english_beats_a_chinese_browser_and_live_settings_are_polled(page):
    assert 'value==="en"||value==="zh"||value==="zh-tw"' in page
    assert 'function refreshOpsLanguage()' in page
    assert 'setInterval(refreshOpsLanguage, 5000)' in page
    assert 'fetch(authUrl("/api/settings"), {cache:"no-store"})' in page
