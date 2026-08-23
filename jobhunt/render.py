"""Render matched jobs as a self-contained, offline HTML digest."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .util import human_age, truncate

_CSS = """
:root{
  --bg:#f6f7f9; --surface:#ffffff; --border:#e3e6ea; --text:#14181d;
  --muted:#5f6b7a; --accent:#1f6feb; --chip:#eef2f7; --badge:#1f6feb;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.04);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0f1216; --surface:#171b21; --border:#272d36; --text:#e6eaef;
    --muted:#94a1b2; --accent:#5b9bff; --chip:#222831; --badge:#3b7dfb;
    --shadow:none;
  }
}
*{box-sizing:border-box}
body{
  margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
.wrap{max-width:900px;margin:0 auto}
header{margin-bottom:24px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0 8px}
input[type=search],select{
  padding:9px 12px;border:1px solid var(--border);border-radius:8px;
  background:var(--surface);color:var(--text);font-size:14px;
}
input[type=search]{flex:1;min-width:220px}
.count{color:var(--muted);font-size:13px;margin-bottom:16px}
.job{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;margin-bottom:12px;box-shadow:var(--shadow);
}
.job.hidden{display:none}
.row{display:flex;gap:12px;align-items:baseline;justify-content:space-between}
.title{font-size:16px;font-weight:600;margin:0}
.title a{color:var(--text);text-decoration:none}
.title a:hover{color:var(--accent);text-decoration:underline}
.badge{
  flex:none;background:var(--badge);color:#fff;font-size:12px;font-weight:600;
  padding:3px 9px;border-radius:20px;white-space:nowrap;
}
.meta{color:var(--muted);font-size:13px;margin-top:5px;
      display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.meta .dot{opacity:.5}
.company{font-weight:600;color:var(--text)}
.snippet{color:var(--muted);font-size:13.5px;margin-top:9px}
.chips{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap}
.flags{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.flag{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:20px;
      border:1px solid var(--border);color:var(--muted)}
.flag.new{background:var(--accent);border-color:var(--accent);color:#fff}
.flag.deadline{background:var(--chip)}
.flag.deadline.urgent{background:#b4441f;border-color:#b4441f;color:#fff}
.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:12px;color:var(--muted)}
.uid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;opacity:.7}
.empty{
  background:var(--surface);border:1px dashed var(--border);border-radius:10px;
  padding:40px;text-align:center;color:var(--muted);
}
footer{margin-top:32px;color:var(--muted);font-size:12.5px;text-align:center}
"""

_JS = """
(function(){
  var q = document.getElementById('q');
  var minScore = document.getElementById('minScore');
  var companyPick = document.getElementById('company');
  var counter = document.getElementById('shown');
  var jobs = Array.prototype.slice.call(document.querySelectorAll('.job'));

  function apply(){
    var term = q.value.trim().toLowerCase();
    var floor = parseFloat(minScore.value) || 0;
    var co = companyPick.value;
    var shown = 0;
    jobs.forEach(function(el){
      var ok = parseFloat(el.dataset.score) >= floor
        && (!co || el.dataset.company === co)
        && (!term || el.dataset.search.indexOf(term) !== -1);
      el.classList.toggle('hidden', !ok);
      if (ok) shown++;
    });
    counter.textContent = shown + (shown === 1 ? ' role' : ' roles');
  }

  [q, minScore, companyPick].forEach(function(el){
    el.addEventListener('input', apply);
    el.addEventListener('change', apply);
  });
  apply();
})();
"""


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def render_digest(
    rows, *, subtitle: str = "", generated_at: str | None = None, since: str | None = None
) -> str:
    """Build the digest page. `rows` are sqlite3.Row/dict-like job records."""
    jobs = [dict(row) for row in rows]
    generated = generated_at or datetime.now().strftime("%d %b %Y, %H:%M")
    companies = sorted({j.get("company", "") for j in jobs if j.get("company")})

    options = "".join(f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in companies)
    body = "\n".join(_job_card(job, since) for job in jobs) if jobs else _empty_state()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job digest — {_esc(generated)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Job digest</h1>
    <div class="sub">{_esc(subtitle)}{" &middot; " if subtitle else ""}generated {_esc(generated)}</div>
  </header>

  <div class="controls">
    <input type="search" id="q" placeholder="Filter by title, company, location, keyword…">
    <select id="company"><option value="">All companies</option>{options}</select>
    <select id="minScore">
      <option value="0">Any score</option>
      <option value="3">Score 3+</option>
      <option value="5">Score 5+</option>
      <option value="10">Score 10+</option>
    </select>
  </div>
  <div class="count"><span id="shown"></span> shown</div>

  {body}

  <footer>Mark one off with <code>jobhunt mark &lt;id&gt; applied</code></footer>
</div>
<script>{_JS}</script>
</body>
</html>
"""


def _job_card(job: dict, since: str | None = None) -> str:
    score = job.get("score") or 0
    score_label = int(score) if float(score).is_integer() else round(float(score), 1)

    try:
        matched = json.loads(job.get("matched") or "[]")
    except (ValueError, TypeError):
        matched = []

    meta_bits = []
    if job.get("company"):
        meta_bits.append(f'<span class="company">{_esc(job["company"])}</span>')
    if job.get("location"):
        meta_bits.append(_esc(job["location"]))
    if job.get("remote"):
        meta_bits.append("Remote")
    if job.get("department"):
        meta_bits.append(_esc(job["department"]))
    age = human_age(job.get("posted_at"))
    if age:
        meta_bits.append(f"posted {_esc(age)} ago")
    meta_bits.append(f'<span class="uid">{_esc(job.get("uid", ""))}</span>')
    meta = '<span class="dot">·</span>'.join(meta_bits)

    flags = ""
    if since and (job.get("first_seen") or "") > since:
        flags += '<span class="flag new">New</span>'
    closing = _closing_label(job.get("deadline"))
    if closing:
        urgent = " urgent" if closing.startswith(("Closes today", "Closes tomorrow")) or (
            closing.startswith("Closes in") and int(closing.split()[2]) <= 7
        ) else ""
        flags += f'<span class="flag deadline{urgent}">{_esc(closing)}</span>'
    

    chips = "".join(f'<span class="chip">{_esc(term)}</span>' for term in matched[:12])
    snippet = truncate(job.get("description") or "", 240)

    search_blob = " ".join(
        str(job.get(field) or "")
        for field in ("title", "company", "location", "department", "description")
    ).lower()

    return f"""  <article class="job" data-score="{_esc(score)}" data-company="{_esc(job.get('company',''))}" data-search="{_esc(search_blob[:1200])}">
    <div class="row">
      <h2 class="title"><a href="{_esc(job.get('url',''))}" target="_blank" rel="noopener">{_esc(job.get('title',''))}</a></h2>
      <span class="badge">{_esc(score_label)}</span>
    </div>
    <div class="meta">{meta}</div>
    {f'<div class="flags">{flags}</div>' if flags else ''}
    {f'<div class="snippet">{_esc(snippet)}</div>' if snippet else ''}
    {f'<div class="chips">{chips}</div>' if chips else ''}
  </article>"""


def _empty_state() -> str:
    return """  <div class="empty">
    <p><strong>Nothing left to triage.</strong></p>
    <p>Every matching role has been marked. Use <code>jobhunt digest --all</code>
       to see them again, or loosen <code>rules</code> in your config.</p>
  </div>"""


def _closing_label(deadline: str | None) -> str:
    """How long is left to apply, where the board says so at all."""
    if not deadline:
        return ""
    try:
        dt = datetime.fromisoformat(deadline)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    days = (dt - datetime.now(timezone.utc)).days
    if days < 0:
        return "Closed"
    if days == 0:
        return "Closes today"
    if days == 1:
        return "Closes tomorrow"
    return f"Closes in {days} days"
