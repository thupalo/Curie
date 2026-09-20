"""Render docs/*.md into one HTML page (docs/curie-docs.html).

    python docs/build_html.py            # writes docs/curie-docs.html
    python docs/build_html.py out.html   # elsewhere

The page carries every chapter in order with a sticky chapter index, in
the same visual language as the project's plan page. It is what gets
published as the documentation artifact; keep the Markdown as the source.
"""
from __future__ import annotations

import html
import re
import sys
from datetime import date
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
ORDER = sorted(p for p in DOCS.glob("[0-9][0-9]-*.md"))

CSS = """
:root{--bg:#F5F7FA;--surface:#FFFFFF;--surface-2:#EDF1F6;--ink:#1C2431;--ink-2:#4A5568;--ink-3:#7B8797;
--line:#D6DDE6;--accent:#2B5D8C;--accent-soft:#E3ECF5;--amber:#A8701A;--amber-soft:#F7EDD9;--ok:#2F7A4D;
--warn:#B4403A;--warn-soft:#F8E6E4;--code-bg:#F0F3F7;--ruler:rgba(43,93,140,.28)}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#121821;--surface:#1A222D;--surface-2:#222C39;
--ink:#E6EBF2;--ink-2:#B4BECB;--ink-3:#7F8B9B;--line:#2E3947;--accent:#7FB0DE;--accent-soft:#1F3247;--amber:#E0A94F;
--amber-soft:#3A2E17;--ok:#6CC08E;--warn:#EF8A82;--warn-soft:#3F2422;--code-bg:#0F141B;--ruler:rgba(127,176,222,.35)}}
:root[data-theme="dark"]{--bg:#121821;--surface:#1A222D;--surface-2:#222C39;--ink:#E6EBF2;--ink-2:#B4BECB;--ink-3:#7F8B9B;
--line:#2E3947;--accent:#7FB0DE;--accent-soft:#1F3247;--amber:#E0A94F;--amber-soft:#3A2E17;--ok:#6CC08E;--warn:#EF8A82;
--warn-soft:#3F2422;--code-bg:#0F141B;--ruler:rgba(127,176,222,.35)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Source Sans 3","Segoe UI",system-ui,sans-serif;font-size:17px;line-height:1.55}
a{color:var(--accent)}
.wrap{display:grid;grid-template-columns:250px minmax(0,1fr);gap:48px;max-width:1240px;margin:0 auto;padding:40px 28px 96px}
nav.toc{position:sticky;top:24px;align-self:start;font-size:14px;max-height:calc(100vh - 48px);overflow:auto}
nav.toc .eyebrow{margin-bottom:10px}
nav.toc ol{list-style:none;margin:0;padding:0;border-left:2px solid var(--line)}
nav.toc li a{display:block;padding:5px 12px;color:var(--ink-2);text-decoration:none;border-left:2px solid transparent;margin-left:-2px}
nav.toc li a:hover,nav.toc li a:focus-visible{color:var(--accent);border-left-color:var(--accent);outline:none}
main{min-width:0;max-width:78ch}
h1,h2,h3,h4{font-family:Archivo,"Segoe UI",system-ui,sans-serif;text-wrap:balance;line-height:1.15}
h1{font-size:2.3rem;font-weight:700;margin:0 0 .4rem;letter-spacing:-.01em}
h2{font-size:1.5rem;font-weight:600;margin:3rem 0 1rem;padding-top:1.2rem;border-top:1px solid var(--line)}
h3{font-size:1.12rem;font-weight:600;margin:2rem 0 .6rem}
h4{font-size:1rem;font-weight:600;margin:1.4rem 0 .4rem}
section.chapter>h2{font-size:1.75rem;margin-top:4rem;border-top:2px solid var(--accent)}
p{margin:0 0 1rem}
.eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);font-weight:600}
.lede{font-size:1.15rem;color:var(--ink-2);max-width:62ch}
.meta{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:14px;color:var(--ink-3);margin:14px 0 0}
.meta b{color:var(--ink-2);font-weight:600}
code,pre{font-family:"IBM Plex Mono",Consolas,monospace}
code{font-size:.88em;background:var(--code-bg);padding:.08em .35em;border-radius:3px}
pre{background:var(--code-bg);border:1px solid var(--line);border-radius:4px;padding:14px 16px;overflow-x:auto;font-size:13.5px;line-height:1.45;margin:0 0 1.2rem}
pre code{background:none;padding:0;font-size:inherit}
table{border-collapse:collapse;width:100%;font-size:15px;background:var(--surface);margin:0 0 1.2rem}
.tablewrap{overflow-x:auto;margin:0 0 1.2rem;border:1px solid var(--line);border-radius:4px}
.tablewrap table{margin:0}
th,td{text-align:left;vertical-align:top;padding:8px 12px;border-bottom:1px solid var(--line)}
th{background:var(--surface-2);font-family:Archivo,sans-serif;font-weight:600;font-size:13px;letter-spacing:.03em}
tr:last-child td{border-bottom:none}
ul,ol{padding-left:1.3rem;margin:0 0 1rem}
li{margin-bottom:.35rem}
blockquote{margin:0 0 1.2rem;padding:10px 16px;border-left:4px solid var(--amber);background:var(--amber-soft)}
hr{border:none;border-top:1px solid var(--line);margin:2rem 0}
@media (max-width:900px){.wrap{grid-template-columns:1fr;gap:24px}nav.toc{position:static;max-height:none}
nav.toc ol{display:flex;flex-wrap:wrap;gap:4px 10px;border:none}nav.toc li a{padding:2px 0;border:none}}
"""

HEAD = """<title>Curie Handbook</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Source+Sans+3:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css}</style>
<div class="wrap">
<nav class="toc" aria-label="Chapters"><div class="eyebrow">Chapters</div><ol>{toc}</ol></nav>
<main>
<div class="eyebrow">Documentation · {date}</div>
<h1>Curie Handbook</h1>
<p class="lede">An LLM chat inside SAP: started from the SAP GUI, reading tables and the dictionary over RFC, writing ABAP reports and running them after a syntax check, a security review and the user's confirmation. R/3 4.5B and ECC 6.</p>
<div class="meta"><span><b>State</b> version 1, verified on R/3 4.5B (TC0)</span><span><b>Source</b> sap_ai_chat/docs/*.md</span><span><b>Bridge</b> NW RFC SDK 7.20, Program ID SAP_AI</span></div>
"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(md_text: str, chapter_id: str) -> tuple[str, str]:
    """HTML of one chapter with its H1 turned into the chapter heading."""
    md_text = re.sub(r"\]\(\.\./[^)]*\)", "]", md_text)          # links into the repo
    md_text = re.sub(r"\]\((\d\d-[a-z0-9-]+)\.md\)", lambda m: f"](#{m.group(1)})", md_text)
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
    title = re.search(r"<h1>(.*?)</h1>", body)
    heading = html.unescape(title.group(1)) if title else chapter_id
    body = re.sub(r"<h1>.*?</h1>", "", body, count=1)
    # demote h2/h3 under the chapter, wrap tables for horizontal scrolling
    body = body.replace("<h3>", "<h4>").replace("</h3>", "</h4>")
    body = body.replace("<h2>", "<h3>").replace("</h2>", "</h3>")
    body = body.replace("<table>", '<div class="tablewrap"><table>').replace("</table>", "</table></div>")
    body = re.sub(r"<a href=\"([^\"]+)\">", lambda m: f'<a href="{m.group(1)}">', body)
    return heading, f'<section class="chapter" id="{chapter_id}"><h2>{html.escape(heading)}</h2>{body}</section>'


def main(out: Path) -> None:
    chapters, toc = [], []
    for path in ORDER:
        chapter_id = path.stem
        heading, section = render(path.read_text(encoding="utf-8"), chapter_id)
        chapters.append(section)
        toc.append(f'<li><a href="#{chapter_id}">{html.escape(heading)}</a></li>')
    page = HEAD.format(css=CSS, toc="".join(toc), date=date.today().isoformat())
    page += "".join(chapters) + "</main></div>\n"
    out.write_text(page, encoding="utf-8")
    print(f"{out}: {len(chapters)} chapters, {len(page)//1024} KB")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DOCS / "curie-docs.html")
