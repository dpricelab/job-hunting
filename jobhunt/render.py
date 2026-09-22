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
.actions{margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.actions button{
  font:inherit;font-size:13px;padding:5px 12px;border-radius:7px;cursor:pointer;
  border:1px solid var(--border);background:var(--surface);color:var(--text);
}
.actions button:hover{border-color:var(--accent);color:var(--accent)}
.actions .result{font-size:12.5px;color:var(--muted)}
.actions .result button{font-size:12px;padding:2px 8px}
.job.done{opacity:.45}
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
    rows,
    *,
    subtitle: str = "",
    generated_at: str | None = None,
    since: str | None = None,
    api_token: str | None = None,
) -> str:
    """Build the digest page. `rows` are sqlite3.Row/dict-like job records."""
    jobs = [dict(row) for row in rows]
    generated = generated_at or datetime.now().strftime("%d %b %Y, %H:%M")
    companies = sorted({j.get("company", "") for j in jobs if j.get("company")})

    options = "".join(f'<option value="{_esc(c)}">{_esc(c)}</option>' for c in companies)
    footer = (
        "Marking a role removes it from this list. Reload to see the current one."
        if api_token
        else "Mark one off with <code>jobhunt mark &lt;id&gt; applied</code>, "
        "or run <code>jobhunt serve</code> for buttons."
    )
    token_meta = (
        f'<meta name="jobhunt-token" content="{_esc(api_token)}">' if api_token else ""
    )
    body = (
        "\n".join(_job_card(job, since, bool(api_token)) for job in jobs)
        if jobs
        else _empty_state()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job digest — {_esc(generated)}</title>
{token_meta}
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

  <footer>{footer}</footer>
</div>
<script>{_JS}</script>
{_ACTION_JS if api_token else ''}
</body>
</html>
"""


def _job_card(job: dict, since: str | None = None, interactive: bool = False) -> str:
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
    

    uid = _esc(job.get("uid", ""))
    was = _esc(job.get("status") or "new")
    actions = (
        f'<div class="actions" data-uid="{uid}" data-was="{was}">'
        f'<button type="button" data-status="applied">Applied</button>'
        f'<button type="button" data-status="ignored">Not for me</button>'
        f'<span class="result"></span>'
        f"</div>"
        if interactive
        else ""
    )

    chips = "".join(f'<span class="chip">{_esc(term)}</span>' for term in matched[:12])
    snippet = truncate(job.get("description") or "", 240)

    search_blob = " ".join(
        str(job.get(field) or "")
        for field in ("title", "company", "location", "department", "description")
    ).lower()

    return f"""  <article class="job" data-score="{_esc(score)}" data-company="{_esc(job.get('company',''))}" data-search="{_esc(search_blob[:1200])}">
    <div class="row">
      <h2 class="title"><a href="{_esc(_safe_href(job.get('url')))}" target="_blank" rel="noopener">{_esc(job.get('title',''))}</a></h2>
      <span class="badge">{_esc(score_label)}</span>
    </div>
    <div class="meta">{meta}</div>
    {f'<div class="flags">{flags}</div>' if flags else ''}
    {f'<div class="snippet">{_esc(snippet)}</div>' if snippet else ''}
    {f'<div class="chips">{chips}</div>' if chips else ''}
    {actions}
  </article>"""


def _safe_href(url) -> str:
    """Only web links become clickable.

    Posting URLs come from whatever a careers page embeds. A `javascript:`
    one would run in the served page, which holds the token that lets the
    page change statuses.
    """
    url = str(url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else "#"


def _empty_state() -> str:
    return """  <div class="empty">
    <p><strong>Nothing left to triage.</strong></p>
    <p>Every matching role has been marked. Use <code>jobhunt digest --all</code>
       to see them again, or loosen <code>rules</code> in your config.</p>
  </div>"""


def _closing_label(deadline: str | None, now: datetime | None = None) -> str:
    """How long is left to apply, where the board says so at all.

    Counted in calendar days, not 24-hour spans. Most boards give a bare date,
    stored as midnight, and that means "open until the end of that day":
    subtracting timestamps called it closed on the deadline day itself.
    """
    if not deadline:
        return ""
    try:
        dt = datetime.fromisoformat(deadline)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)

    date_only = (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)
    if date_only:
        days = (dt.date() - now.astimezone(timezone.utc).date()).days
    else:
        if dt <= now:
            return "Closed"
        days = (dt.astimezone(timezone.utc).date() - now.astimezone(timezone.utc).date()).days
    if days < 0:
        return "Closed"
    if days == 0:
        return "Closes today"
    if days == 1:
        return "Closes tomorrow"
    return f"Closes in {days} days"


_ACTION_JS = """<script>
(function(){
  var meta = document.querySelector('meta[name="jobhunt-token"]');
  if (!meta) return;
  var token = meta.content;

  function send(uid, status){
    return fetch('/api/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Jobhunt-Token': token},
      body: JSON.stringify({uid: uid, status: status})
    }).then(function(r){ return r.json(); }).then(function(data){
      if (!data.ok) throw new Error(data.detail || 'failed');
      return data;
    });
  }

  function failed(result){
    return function(err){
      result.textContent = (err && err.message && err.message !== 'Failed to fetch')
        ? err.message
        : 'could not reach jobhunt — is it still running?';
    };
  }

  function mark(card, box, status, result){
    result.textContent = 'saving…';
    send(box.dataset.uid, status).then(function(){
      card.classList.add('done');
      result.innerHTML = '';
      result.appendChild(document.createTextNode('Marked ' + status + '. '));
      var undo = document.createElement('button');
      undo.type = 'button';
      undo.textContent = 'Undo';
      undo.addEventListener('click', function(){ restore(card, box, result); });
      result.appendChild(undo);
    }).catch(failed(result));
  }

  // Undo puts back whatever the role was before, which is not always 'new'
  // (with --all the page also shows roles already triaged), and leaves the
  // card looking untouched rather than marked all over again.
  function restore(card, box, result){
    result.textContent = 'saving…';
    send(box.dataset.uid, box.dataset.was || 'new').then(function(){
      card.classList.remove('done');
      result.textContent = '';
    }).catch(failed(result));
  }

  document.querySelectorAll('.actions').forEach(function(box){
    var result = box.querySelector('.result');
    box.querySelectorAll('button[data-status]').forEach(function(button){
      button.addEventListener('click', function(){
        mark(box.closest('.job'), box, button.dataset.status, result);
      });
    });
  });
})();
</script>"""
