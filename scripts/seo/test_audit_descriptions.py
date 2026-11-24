import tempfile
import unittest
from pathlib import Path

from audit_descriptions import audit


DESCRIPTION = "Jun Kang의 블로그입니다. PostgreSQL 운영, 백엔드 개발, AI 도입과 개발팀 성장에 관한 글을 모았습니다."


class AuditDescriptionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, path, html):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(html, encoding="utf-8")

    def page(self, path, description, social=None, extra=""):
        if social is None:
            social = description
        self.write(
            path,
            f'<html><head><meta name=description content="{description}">'
            f'<meta property=og:description content="{social}">'
            f'<meta name=twitter:description content="{social}">{extra}</head></html>',
        )

    def test_reports_reused_descriptions_and_excludes_redirect_and_verification(self):
        self.page("index.html", DESCRIPTION)
        self.page("page/2/index.html", DESCRIPTION)
        self.page("tags/postgresql/index.html", DESCRIPTION, extra="<meta name=robots content=noindex>")
        self.write("page/1/index.html", '<head><meta http-equiv=refresh content="0; url=/"></head>')
        self.write("naver123.html", "naver-site-verification: naver123.html")
        report = audit(self.root)
        self.assertEqual(report["counts"]["all"]["duplicate"], 3)
        self.assertEqual(report["counts"]["all"]["fallback_or_markup"], 2)
        self.assertEqual(report["counts"]["indexable: home pagination"]["pages"], 1)
        self.assertEqual(report["counts"]["noindex: tags term"]["pages"], 1)
        self.assertEqual(report["skipped"]["redirects"], ["/page/1/"])
        self.assertEqual(report["skipped"]["verification_files"], ["/naver123.html"])

    def test_checks_each_channel_and_flags_malformed_metadata(self):
        self.page("about/index.html", "**About**", "가" * 161)
        self.write(
            "archives/index.html",
            '<head><meta name=description content=archives><meta name=description content="">'
            '<meta property=og:title content=Archives></head>',
        )
        report = audit(self.root)
        self.assertEqual(
            report["pages"]["/about/"]["issues"],
            {
                "too_short": ["description"],
                "fallback_or_markup": ["description"],
                "too_long": ["og:description", "twitter:description"],
                "mismatch": ["og:description", "twitter:description"],
            },
        )
        self.assertEqual(
            report["pages"]["/archives/"]["issues"]["missing"],
            ["description", "og:description", "twitter:description"],
        )
        self.assertEqual(report["pages"]["/archives/"]["issues"]["repeated_tag"], ["description"])
        self.assertEqual(report["pages"]["/archives/"]["issues"]["fallback_or_markup"], ["description"])

    def test_accepts_matching_decoded_values_and_distinct_pagination(self):
        self.page("index.html", DESCRIPTION + " 검색 &amp; 활용")
        self.page("page/2/index.html", DESCRIPTION + " 검색 &#38; 활용 (2페이지)")
        report = audit(self.root)
        self.assertEqual(report["counts"]["all"], {"pages": 2})
        self.assertIn(" & ", report["pages"]["/"]["descriptions"]["description"][0])


if __name__ == "__main__":
    unittest.main()
