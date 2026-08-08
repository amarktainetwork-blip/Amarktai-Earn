from __future__ import annotations

import json
import os
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""; self._title = False; self.text = []; self.headings = []
        self.meta_description = ""; self.lang = ""; self.canonical = ""
        self.links = 0; self.images = 0; self.images_without_alt = 0

    def handle_starttag(self, tag, attrs):
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        tag = tag.casefold()
        if tag == "html": self.lang = values.get("lang", "")
        if tag == "title": self._title = True
        if tag == "meta" and values.get("name", "").casefold() == "description": self.meta_description = values.get("content", "")
        if tag == "link" and values.get("rel", "").casefold() == "canonical": self.canonical = values.get("href", "")
        if tag in {"a", "link"}: self.links += 1
        if tag == "img":
            self.images += 1
            if not values.get("alt", "").strip(): self.images_without_alt += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}: self.headings.append(tag)

    def handle_endtag(self, tag):
        if tag.casefold() == "title": self._title = False

    def handle_data(self, data):
        cleaned = data.strip()
        if cleaned:
            self.text.append(cleaned)
            if self._title: self.title += (" " if self.title else "") + cleaned


class SEOAuditWorker(Worker):
    worker_class = "seo_audit"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "seo_content_audit":
                return WorkResult(ok=False, error="unsupported SEO operation")
            source = Path(str(request.inputs["source"]))
            maximum = max(1024, int(os.getenv("SEO_MAX_HTML_BYTES", str(5 * 1024 * 1024))))
            if source.stat().st_size > maximum:
                return WorkResult(ok=False, error="SEO_HTML_SIZE_LIMIT")
            parser = _PageParser(); parser.feed(source.read_text(encoding="utf-8", errors="replace"))
            body = " ".join(parser.text)
            words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", body.casefold())
            stop = {"the", "and", "for", "that", "with", "this", "from", "are", "was", "you", "your", "have", "has"}
            keywords = Counter(word for word in words if word not in stop).most_common(20)
            target_keywords = [str(value).strip().casefold() for value in request.inputs.get("target_keywords", []) if str(value).strip()]
            missing_keywords = [keyword for keyword in target_keywords if keyword not in body.casefold()]
            findings = []
            if not parser.title: findings.append("TITLE_MISSING")
            elif not 20 <= len(parser.title) <= 70: findings.append("TITLE_LENGTH_REVIEW")
            if not parser.meta_description: findings.append("META_DESCRIPTION_MISSING")
            elif not 50 <= len(parser.meta_description) <= 180: findings.append("META_DESCRIPTION_LENGTH_REVIEW")
            if parser.headings.count("h1") != 1: findings.append("H1_COUNT_REVIEW")
            if parser.images_without_alt: findings.append("IMAGE_ALT_MISSING")
            if not parser.lang: findings.append("HTML_LANGUAGE_MISSING")
            if not parser.canonical: findings.append("CANONICAL_MISSING")
            payload = {
                "title": parser.title, "meta_description": parser.meta_description, "language": parser.lang,
                "canonical": parser.canonical, "word_count": len(words), "headings": dict(Counter(parser.headings)),
                "links": parser.links, "images": parser.images, "images_without_alt": parser.images_without_alt,
                "top_keywords": [{"keyword": key, "count": count} for key, count in keywords],
                "target_keywords": target_keywords, "missing_target_keywords": missing_keywords,
                "findings": findings,
                "policy": {"spam_or_backlink_automation": False, "source": "supplied_or_prior_policy_approved_artifact"},
            }
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "seo-content-audit.json"
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return WorkResult(ok=True, artifacts=[target], evidence={
                "operation": "seo_content_audit", "word_count": len(words), "finding_count": len(findings),
                "title_present": bool(parser.title), "structured_report": True, "missing_target_keyword_count": len(missing_keywords),
            })
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
