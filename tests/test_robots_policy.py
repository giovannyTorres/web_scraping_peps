"""Offline rules from actual discovery probes plus RFC 9309 edge cases."""
import unittest

from peps.robots_policy import robots_allowed


class RobotsPolicyTests(unittest.TestCase):
    agent = "WebScope-Live-Validation/2.8 (+https://example.test/contact)"

    def allowed(self, body, path, user_agent=None):
        return robots_allowed(body, user_agent or self.agent, "https://example.test" + path)

    def test_real_opnxng_query_wildcard_blocks_queries(self):
        # https://opnxng.com/robots.txt, observed 2026-10-16, HTTP200.
        body = """User-agent: *
Allow: /info/en/about
Disallow: /stats
Disallow: /image
Disallow: /preferences
Disallow: /*?*q=*
"""
        for path in ("/search?q=Pe%C3%B1a&format=json", "/?format=json&q=Pemex", "/search?x=1&q="):
            with self.subTest(path=path):
                self.assertFalse(self.allowed(body, path))
        self.assertTrue(self.allowed(body, "/info/en/about"))
        self.assertTrue(self.allowed(body, "/search?format=json"))

    def test_real_commoncrawl_end_anchors_and_wildcard_allow(self):
        # https://index.commoncrawl.org/robots.txt, observed 2026-10-16.
        body = """User-agent: *
Disallow: /
Allow: /$
Allow: /index.html$
Allow: /web-graphs-index.html$
Allow: /collinfo.json$
Allow: /graphinfo.json$
Allow: /ccbot.json$
Allow: /.well-known/*.txt$
"""
        for path in ("/", "/collinfo.json", "/.well-known/security.txt"):
            with self.subTest(path=path):
                self.assertTrue(self.allowed(body, path))
        for path in ("/CC-MAIN-2026-21-index?url=example.test", "/collinfo.json?x=1", "/index.html/extra", "/.well-known/security.txt/extra"):
            with self.subTest(path=path):
                self.assertFalse(self.allowed(body, path))

    def test_longest_rule_wins_independently_of_order(self):
        for rules in ("Allow: /private/public\nDisallow: /private", "Disallow: /private\nAllow: /private/public"):
            self.assertTrue(self.allowed("User-agent: *\n" + rules, "/private/public/page"))
            self.assertFalse(self.allowed("User-agent: *\n" + rules, "/private/page"))

    def test_allow_wins_equal_length_even_with_wildcards(self):
        for rules in ("Disallow: /a*b\nAllow: /ab", "Allow: /ab\nDisallow: /a*b"):
            self.assertTrue(self.allowed("User-agent: *\n" + rules, "/ab"))

    def test_wildcards_and_end_anchor_do_not_inflate_specificity(self):
        body = "User-agent: *\nDisallow: /a****************$\nAllow: /a"
        self.assertTrue(self.allowed(body, "/a"))

    def test_path_case_is_sensitive(self):
        body = "User-agent: *\nDisallow: /Private"
        self.assertFalse(self.allowed(body, "/Private"))
        self.assertTrue(self.allowed(body, "/private"))

    def test_agent_specificity_and_case_insensitive_product_substring(self):
        body = "User-agent: *\nDisallow: /\nUser-agent: Scope\nDisallow: /restricted\nUser-agent: WEBSCOPE-LIVE\nAllow: /"
        self.assertTrue(self.allowed(body, "/restricted"))
        self.assertFalse(self.allowed(body, "/", "OtherBot/1.0"))

    def test_equally_specific_groups_are_combined(self):
        body = "User-agent: WebScope\nDisallow: /private\nUser-agent: WEBSCOPE\nAllow: /private/public"
        self.assertFalse(self.allowed(body, "/private"))
        self.assertTrue(self.allowed(body, "/private/public"))

    def test_all_default_groups_are_combined_when_no_specific_group(self):
        body = "User-agent: *\nDisallow: /private\nUser-agent: OtherBot\nAllow: /\nUser-agent: *\nAllow: /private/public"
        self.assertFalse(self.allowed(body, "/private"))
        self.assertTrue(self.allowed(body, "/private/public"))

    def test_empty_specific_group_overrides_default_restriction(self):
        body = "User-agent: *\nDisallow: /\nUser-agent: WebScope\nDisallow:\nAllow:"
        self.assertTrue(self.allowed(body, "/anything"))

    def test_consecutive_agents_empty_lines_unknown_records_and_comments(self):
        body = "\ufeffUSER-AGENT: FirstBot\r\n\r\nSitemap: https://example.test/map\r\nUser-agent: WebScope # named crawler\r\nDisallow: /private # comment\r\nAllow: /private/open\r\n"
        self.assertFalse(self.allowed(body, "/private", "FirstBot/1.0"))
        self.assertFalse(self.allowed(body, "/private"))
        self.assertTrue(self.allowed(body, "/private/open"))

    def test_product_version_and_contact_do_not_select_unrelated_groups(self):
        body = "User-agent: *\nDisallow: /\nUser-agent: contact\nAllow: /"
        self.assertFalse(self.allowed(body, "/"))

    def test_empty_rules_or_rules_before_an_agent_impose_no_restriction(self):
        for body in ("", "Disallow: /", "User-agent: *\nDisallow:\nAllow:", "User-agent: OtherBot\nDisallow: /"):
            with self.subTest(body=body):
                self.assertTrue(self.allowed(body, "/"))

    def test_unreserved_percent_escapes_are_decoded_in_rules_and_url(self):
        body = "User-agent: *\nDisallow: /foo/%62%61%7a"
        for path in ("/foo/baz", "/foo/%62%61%7A"):
            self.assertFalse(self.allowed(body, path))

    def test_utf8_percent_octets_match_literal_unicode(self):
        for rule in ("/Peña/ツ", "/Pe%C3%B1a/%e3%83%84"):
            body = "User-agent: *\nDisallow: " + rule
            for path in ("/Peña/ツ", "/Pe%c3%b1a/%E3%83%84"):
                with self.subTest(rule=rule, path=path):
                    self.assertFalse(self.allowed(body, path))

    def test_encoded_reserved_separator_stays_distinct_from_literal_separator(self):
        body = "User-agent: *\nDisallow: /foo%2Fbar"
        self.assertFalse(self.allowed(body, "/foo%2fbar"))
        self.assertTrue(self.allowed(body, "/foo/bar"))
        self.assertTrue(self.allowed("User-agent: *\nDisallow: /foo/bar", "/foo%2Fbar"))

    def test_percent_encoded_wildcard_anchor_and_hash_are_literals(self):
        body = "User-agent: *\nDisallow: /foo/%2A\nDisallow: /end%24\nDisallow: /hash%23part"
        for path in ("/foo/%2a", "/end%24more", "/hash%23part"):
            self.assertFalse(self.allowed(body, path))
        for path in ("/foo/bar", "/end", "/hash#part"):
            self.assertTrue(self.allowed(body, path))

    def test_percent_encoding_and_unicode_length_are_counted_as_octets(self):
        # /é is three octets, /abc is four; wildcard and % notation add none.
        body = "User-agent: *\nAllow: /%C3%A9*\nDisallow: /*abc"
        self.assertFalse(self.allowed(body, "/éabc"))
        # A three-octet Unicode rule beats a two-octet ASCII rule.
        body = "User-agent: *\nDisallow: /é*\nAllow: /*a"
        self.assertFalse(self.allowed(body, "/éa"))

    def test_query_participates_in_matching_and_fragment_does_not(self):
        body = "User-agent: *\nDisallow: /search?q=private$\nDisallow: /empty?$"
        self.assertFalse(self.allowed(body, "/search?q=private#ignored"))
        self.assertTrue(self.allowed(body, "/search?q=private&other=1"))
        self.assertFalse(self.allowed(body, "/empty?"))
        self.assertTrue(self.allowed(body, "/empty"))

    def test_empty_url_path_is_root(self):
        self.assertFalse(robots_allowed("User-agent: *\nDisallow: /$", self.agent, "https://example.test"))

    def test_multiple_wildcards_are_bounded_and_terminal_anchor_checks_last_match(self):
        body = "User-agent: *\nDisallow: /foo*bar*bar$"
        self.assertFalse(self.allowed(body, "/foo/firstbar/lastbar"))
        self.assertTrue(self.allowed(body, "/foo/firstbar/lastbar/extra"))
        adversarial = "User-agent: *\nDisallow: /" + "*a" * 1500 + "b$"
        self.assertTrue(self.allowed(adversarial, "/" + "a" * 3000))

    def test_invalid_input_and_raw_header_controls_fail_closed(self):
        for url in ("https://example.test/\r\nX:bad", "https://example.test/\tpath", "https://example.test/with space", "https://[invalid/", "https://example.test:99999/", "https://user:secret@example.test/", "file:///etc/passwd", "/relative", "https://example.test/\ud800"):
            with self.subTest(url=repr(url)):
                self.assertFalse(robots_allowed("", self.agent, url))
        for agent in ("", "\r\nOtherBot", "WebScope\x00/1.0"):
            self.assertFalse(robots_allowed("", agent, "https://example.test/"))
        self.assertFalse(robots_allowed(None, self.agent, "https://example.test/"))

    def test_encoded_controls_are_literal_url_octets(self):
        body = "User-agent: *\nDisallow: /path%0A"
        self.assertFalse(self.allowed(body, "/path%0a"))
        self.assertTrue(self.allowed(body, "/path"))

    def test_nonterminal_dollar_is_literal(self):
        body = "User-agent: *\nDisallow: /price$today"
        self.assertFalse(self.allowed(body, "/price$today"))
        self.assertTrue(self.allowed(body, "/pricetoday"))


if __name__ == "__main__":
    unittest.main()
