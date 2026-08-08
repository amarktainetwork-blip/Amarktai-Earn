from __future__ import annotations

from html import escape
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker


class StaticWebOutputWorker(Worker):
    worker_class = "web_output"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "static_html_create":
                return WorkResult(ok=False, error="unsupported web-output operation")
            title = str(request.inputs.get("title") or "Static Page").strip()[:200]
            language = str(request.inputs.get("language") or "en").strip()[:12]
            sections = request.inputs.get("sections")
            if not isinstance(sections, list):
                source = request.inputs.get("source")
                text = Path(str(source)).read_text(encoding="utf-8", errors="replace") if source else str(request.inputs.get("content") or "")
                sections = [{"heading": title, "body": text}]
            normalized = []
            for row in sections[:50]:
                if not isinstance(row, dict): continue
                heading = str(row.get("heading") or "Section").strip()[:200]
                body = str(row.get("body") or "").strip()[:20000]
                normalized.append((heading, body))
            if not normalized:
                return WorkResult(ok=False, error="at least one page section is required")
            request.workspace.mkdir(parents=True, exist_ok=True)
            html_path = request.workspace / "index.html"; css_path = request.workspace / "styles.css"
            blocks = []
            for heading, body in normalized:
                paragraphs = "".join(f"<p>{escape(line)}</p>" for line in body.splitlines() if line.strip()) or "<p></p>"
                blocks.append(f'<section aria-labelledby="{escape(heading.casefold().replace(" ", "-")[:80])}"><h2 id="{escape(heading.casefold().replace(" ", "-")[:80])}">{escape(heading)}</h2>{paragraphs}</section>')
            html = (
                "<!doctype html>\n"
                f'<html lang="{escape(language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{escape(title)}</title><link rel=\"stylesheet\" href=\"styles.css\"></head>"
                f"<body><a class=\"skip\" href=\"#main\">Skip to content</a><header><h1>{escape(title)}</h1></header>"
                f"<main id=\"main\">{''.join(blocks)}</main><footer><p>Static artifact; no deployment performed.</p></footer></body></html>\n"
            )
            css = (
                ":root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.55}body{max-width:72rem;margin:auto;padding:1.5rem}"
                "header,section,footer{margin-block:2rem}.skip{position:absolute;left:-9999px}.skip:focus{left:1rem;top:1rem}"
                "h1,h2{line-height:1.2}p{max-width:75ch}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}\n"
            )
            html_path.write_text(html, encoding="utf-8"); css_path.write_text(css, encoding="utf-8")
            return WorkResult(ok=True, artifacts=[html_path, css_path], evidence={
                "operation": "static_html_create", "title": title, "section_count": len(normalized),
                "language": language, "has_h1": True, "has_skip_link": True,
                "external_scripts": 0, "deployment_performed": False,
            })
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
