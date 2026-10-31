"""P4 third cut: the model-facing provider for the job queue (offline first).

The queue in tools/jobqueue.py has exactly one seam that may talk to a model:
the callable it stores as ``provider(job, hint) -> dict``.  This module holds the
pieces of that seam so the queue itself stays offline and testable:

* ``build_prompt(job, hint=None)`` -- the three sections of docs/Mlog辅助-规划.md
  section 4.4: world facts (snapshot summary + the link whitelist), a short rule
  summary, and the task with the JSON contract.
* ``parse_reply(text)`` -- pull one JSON object out of a model reply (fenced code
  blocks and prose around it are tolerated) and validate the contract.
* ``HttpProvider`` -- an OpenAI-compatible ``/chat/completions`` call over urllib.
  Nothing here touches the network at import time, and the queue never builds one
  unless the CLI was told to (``--jobs-provider llm --llm-url ...``).
* ``ScriptedProvider`` -- replay recorded replies from a JSON file, so every test
  and every regression run stays offline and deterministic.
* ``Recorder`` -- wrap any provider and append ``{prompt, reply}`` JSONL, so a real
  transcript can be replayed later as a scripted one.
"""

import io
import json
import os
import time
import urllib.error
import urllib.request

MAX_PROMPT_CELLS = 40

#: v0.14.0: the whole 'previous version' section, title included, must fit in this
#: many characters -- a workspace whose prevCode is 4000 characters long must not
#: push the actual task out of the model's attention (or out of a small context).
#: One character of the budget is held back for the newline that separates the
#: section from '=== task ===', so the section as it appears in the prompt is
#: never longer than PREV_VERSION_MAX.
PREV_VERSION_MAX = 1200
PREV_FINDINGS_MAX = 6
PREV_TRUNC = '\n  ... (truncated)'


class ProviderError(Exception):
    """A reply that cannot be used: bad JSON, missing keys, wrong types."""


def _link_names(links):
    out = []
    for l in links or []:
        if isinstance(l, dict) and l.get("name"):
            out.append(str(l["name"]))
        elif isinstance(l, str):
            out.append(l)
    return out


def _num(v):
    """120.0 -> '120', 12.5 -> '12.5' (a stock is a number, not a float repr)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - int(f)) < 1e-9:
        return str(int(f))
    return ("%.2f" % f).rstrip("0").rstrip(".")


def _top(d, n):
    """Biggest first, name as the tiebreak, so the prompt is stable across runs."""
    return sorted(d.items(), key=lambda kv: (-float(kv[1]), str(kv[0])))[:n]


def snapshot_facts(snap):
    """One short block of world facts out of a region snapshot (never the whole grid)."""
    if not isinstance(snap, dict):
        return ["(no snapshot was sent; assume the processor is alone in an empty area)"]
    rows = []
    b = snap.get("bounds") or {}
    if isinstance(b, dict) and b:
        rows.append("selection: x %s..%s, y %s..%s" % (b.get("x1"), b.get("x2"),
                                                     b.get("y1"), b.get("y2")))
    rows.append("cells in snapshot: %s (truncated: %s)" % (snap.get("cells"),
                                                          bool(snap.get("truncated"))))
    # v0.12.9: the mod now sends block counts itself (tile counts, empty tiles skipped).
    # Older snapshots only have the grid, so keep the fallback.
    blocks = snap.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        blocks = {}
        for c in snap.get("grid") or []:
            if isinstance(c, dict):
                k = str(c.get("block") or "air")
                if k != "air":
                    blocks[k] = blocks.get(k, 0) + 1
    if blocks:
        top = _top(blocks, 8)
        rows.append("blocks nearby: " + ", ".join("%s x%s" % (k, _num(v)) for k, v in top))
    items = snap.get("items")
    if isinstance(items, dict) and items:
        rows.append("items stored in the area: "
                    + ", ".join("%s %s" % (k, _num(v)) for k, v in _top(items, 8)))
    liquids = snap.get("liquids")
    if isinstance(liquids, dict) and liquids:
        rows.append("liquids stored in the area: "
                    + ", ".join("%s %s" % (k, _num(v)) for k, v in _top(liquids, 4)))
    units = snap.get("units")
    if isinstance(units, dict) and units.get("total"):
        types = units.get("types") if isinstance(units.get("types"), dict) else {}
        teams = units.get("teams") if isinstance(units.get("teams"), dict) else {}
        rows.append("units in the area: %s (%s; teams: %s)" % (
            _num(units.get("total")),
            ", ".join("%s x%s" % (k, _num(v)) for k, v in _top(types, 6)) or "?",
            ", ".join("%s %s" % (k, _num(v)) for k, v in _top(teams, 4)) or "?"))
    enemies = snap.get("enemies")
    if isinstance(enemies, dict) and (enemies.get("units") or enemies.get("blocks")):
        types = enemies.get("types") if isinstance(enemies.get("types"), dict) else {}
        rows.append("enemies in the area: %s unit(s)%s, %s enemy building(s)" % (
            _num(enemies.get("units")),
            (" " + ", ".join("%s x%s" % (k, _num(v)) for k, v in _top(types, 4)))
            if types else "",
            _num(enemies.get("blocks"))))
    procs = snap.get("processors") or []
    if procs:
        rows.append("other processors in the snapshot: %d" % len(procs))
    m = snap.get("map")
    if isinstance(m, dict) and m.get("name"):
        rows.append("map: %s" % m.get("name"))
    return rows


def previous_version(job):
    """The 'improve THIS, do not start over' block, or '' when there is none (v0.14.0).

    A player who says "continue improving" has a program in the processor that
    they want kept: without the previous version in front of it the model happily
    writes a fresh program that drops whatever already worked (and, on a repeat
    run, wanders back into the shape the analyzer already refused).  Only
    mode='improve' with a non-empty workspace.prevCode produces the block, so a
    'new'/'edit' prompt is byte for byte what it was before this existed.
    """
    if not isinstance(job, dict) or job.get("mode") != "improve":
        return ""
    ws = job.get("workspace")
    if not isinstance(ws, dict):
        return ""
    code = ws.get("prevCode")
    if not isinstance(code, str) or not code.strip():
        return ""
    rows = ["=== previous version (same area) ==="]
    for line in code.splitlines():
        rows.append("  " + line)
    findings = ws.get("prevFindings")
    if isinstance(findings, list) and findings:
        for f in findings[:PREV_FINDINGS_MAX]:
            rows.append("  - %s" % (f,))
    prev_job = ws.get("prevJob")
    if isinstance(prev_job, str) and prev_job:
        rows.append("previous job: %s" % prev_job)
    rows.append("- The player wants THIS version improved, not a rewrite from zero.")
    rows.append("- Keep what works; change only what the request needs. "
                "Do not go back to a shape that already failed.")
    text = "\n".join(rows)
    # the blank line that separates this block from '=== task ===' is part of the
    # section as the reader (and the test) sees it: hold back its two newlines
    room = PREV_VERSION_MAX - 2
    if len(text) > room:
        text = text[:room - len(PREV_TRUNC)] + PREV_TRUNC
    return text


def build_prompt(job, hint=None):
    """The three sections of section 4.4, as one string."""
    links = _link_names(job.get("links"))
    origin = job.get("origin") or {}
    ipt = job.get("ipt") or 2
    out = []
    out.append("=== world facts ===")
    if origin:
        out.append("processor tile: (%s, %s); link coordinates are RELATIVE to it" % (
            origin.get("x"), origin.get("y")))
    out.append("instruction set: v160 (ipt=%s)" % ipt)
    out.extend(snapshot_facts(job.get("snapshot")))
    if job.get("links") is None:
        out.append("link whitelist: UNKNOWN (the game could not read the link table).")
        out.append("  Do not invent link names; prefer code that needs no links at all.")
    elif links:
        out.append("link whitelist (the ONLY names allowed as link variables):")
        for l in job.get("links") or []:
            if isinstance(l, dict):
                out.append("  %s @ (%s, %s)" % (l.get("name"), l.get("x"), l.get("y")))
        out.append("  Any other name (container9, core, message2 ...) will be rejected.")
    else:
        out.append("link whitelist: EMPTY -- this processor is wired to nothing.")
        out.append("  Any block-link name in the program will be rejected.")
    out.append("")
    out.append("=== rules ===")
    out.append("- mlog only, one statement per line, no comments, no blank-line tricks.")
    out.append("- A variable is a name used as an operand; assign it before reading it.")
    out.append("- jump targets: NEVER count line numbers. Write a label on its own line")
    out.append("  (`wait_ready:` -- nothing else on that line) and jump to the label:")
    out.append("  `jump wait_ready always`. The game resolves labels when the program loads;")
    out.append("  a numeric target that is off by one silently jumps INTO the middle of a")
    out.append("  block, so the statements it skipped never run. Every jump in your reply")
    out.append("  must name a label you defined.")
    out.append("- print/printflush: print writes into the buffer, printflush sends it.")
    out.append("- sensor <result> <block> <property> reads a property of a linked block.")
    out.append("- control/radar/ucontrol need the documented argument count for v160.")
    out.append("- End the program with an explicit `end` unless it loops forever.")
    out.append("- The whole program must pass the analyzer: any ERR rejects the job.")
    out.append("")
    prev = previous_version(job)
    if prev:
        out.append(prev)
        out.append("")
    out.append("=== task ===")
    out.append("the player asked for: %s" % (job.get("desc") or ""))
    if hint:
        out.append("")
        out.append("your previous attempt was REJECTED by the gate; fix these findings:")
        for f in hint[:12]:
            if isinstance(f, dict):
                out.append("  line %s: [%s] %s" % (f.get("line"), f.get("sev"), f.get("msg")))
            else:
                out.append("  %s" % (f,))
    out.append("")
    out.append("=== reply ===")
    out.append("Reply with ONE JSON object and nothing else (no prose, no markdown fence).")
    out.append("Anchored tier (pick a known template):")
    out.append('  {"template": "item-threshold", "params": {...}, "explanation": "..."}')
    out.append("Free tier (write the program yourself):")
    out.append('  {"code": "set n 0\\nop add n n 1\\nend", "explanation": "...",')
    out.append('   "assumptions": ["..."], "needs_links": ["container1"]}')
    out.append("Program shape for the free tier -- a label loop, no numeric jumps:")
    out.append("  loop:")
    out.append("    sensor n container1 @copper")
    out.append("    jump done greaterThanEq n 100")
    out.append("    op add n n 1")
    out.append("    jump loop always")
    out.append("  done:")
    out.append("    end")
    out.append("(the `code` string uses \\n between statements; label lines are statements'")
    out.append(" landmarks, not statements, and are kept verbatim.)")
    out.append("needs_links must be a subset of the whitelist above (or empty).")
    return "\n".join(out)


def _first_object(text):
    """The first balanced {...} run in text, or None. Tolerates fences and prose."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start:i + 1]
    return None


def parse_reply(text):
    """Model reply -> provider dict. Raises ProviderError when unusable."""
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("empty reply")
    blob = _first_object(text)
    if blob is None:
        raise ProviderError("no JSON object in the reply")
    try:
        obj = json.loads(blob)
    except ValueError as exc:
        raise ProviderError("reply JSON does not parse: %s" % exc)
    if not isinstance(obj, dict):
        raise ProviderError("reply JSON is %s, want an object" % type(obj).__name__)
    code = obj.get("code")
    tid = obj.get("template")
    if not (isinstance(code, str) and code.strip()) and not tid:
        raise ProviderError("reply has neither a non-empty code nor a template")
    if code is not None and not isinstance(code, str):
        raise ProviderError("code must be a string")
    if tid is not None and not isinstance(tid, str):
        raise ProviderError("template must be a string")
    params = obj.get("params")
    if params is not None and not isinstance(params, dict):
        raise ProviderError("params must be an object")
    for k in ("assumptions", "needs_links"):
        v = obj.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise ProviderError("%s must be a list of strings" % k)
    out = {"explanation": str(obj.get("explanation") or ""),
           "assumptions": list(obj.get("assumptions") or []),
           "needs_links": list(obj.get("needs_links") or [])}
    if isinstance(code, str) and code.strip():
        out["code"] = code
    if tid:
        out["template"] = tid
        out["params"] = dict(params or {})
    return out


class ScriptedProvider:
    """Replay recorded replies: replies[i] answers the (i+1)-th attempt."""

    def __init__(self, replies, label="scripted"):
        self.replies = list(replies or [])
        self.label = label
        self.prompts = []

    def __call__(self, job, hint=None):
        prompt = build_prompt(job, hint)
        self.prompts.append(prompt)
        if not self.replies:
            return {"error": "scripted provider ran out of replies"}
        item = self.replies.pop(0)
        if isinstance(item, str):
            try:
                return parse_reply(item)
            except ProviderError as exc:
                return {"error": str(exc)}
        if isinstance(item, dict):
            if item.get("raw") is not None:
                try:
                    return parse_reply(str(item["raw"]))
                except ProviderError as exc:
                    return {"error": str(exc)}
            return dict(item)
        return {"error": "scripted reply of type %s" % type(item).__name__}


def load_script(path):
    """Read a replay file: a list of replies, or {"replies": [...]}."""
    with io.open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("replies") or []
    if not isinstance(data, list):
        raise ValueError("a replay file must hold a list of replies")
    return ScriptedProvider(data)


class Recorder:
    """Wrap a provider and append {prompt, reply} to a JSONL file."""

    def __init__(self, inner, path):
        self.inner = inner
        self.path = path

    def __call__(self, job, hint=None):
        prompt = build_prompt(job, hint)
        out = self.inner(job, hint)
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d and not os.path.isdir(d):
                os.makedirs(d)
            rec = {"desc": job.get("desc"), "prompt": prompt, "reply": out}
            with io.open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return out


DEFAULT_MAX_TOKENS = 4096
TOKENS_CEILING = 16000

#: how much of reasoning_content a reply carries back to the job (the panel shows
#: a screenful of it; the point is to explain a refusal, not to archive a transcript)
REASONING_MAX = 12000

# -- failure classes (v0.15.0) -------------------------------------------------
# One string ('model endpoint answered with an empty message') was all the queue
# ever saw, and it retried that exactly the way it retried a 401: same prompt, same
# budget, no pause.  A job then ended as 'refused after 3 attempt(s)' and the player
# could not tell a wrong key from a flaky proxy.  Every failure now carries a kind,
# the kind decides whether another try can possibly help, and the pause before a
# retry grows.
ERR_TRANSPORT = "transport"      # the endpoint could not be reached at all
ERR_HTTP = "http-status"         # the endpoint answered with an error status
ERR_BAD_JSON = "bad-json"        # it answered, but not with JSON
ERR_EMPTY = "empty"              # a message that carried no content
ERR_TRUNCATED = "truncated"      # the token ceiling ended the reply
ERR_PARSE = "unparsable"         # text came back, but not the JSON contract
ERR_CONFIG = "config"            # nothing to call: retrying cannot change that

#: kinds where repeating the request can plausibly work (transient by nature)
RETRYABLE = (ERR_TRANSPORT, ERR_EMPTY)
#: kinds where another request gives the same answer (a wrong key, no url)
FATAL = (ERR_CONFIG,)
#: seconds to wait before try 2, try 3; the last value repeats
BACKOFF = (0.8, 2.0, 4.0)
#: one attempt plus the retries the provider itself is willing to spend
RETRY_TOTAL = 3
#: how many times the budget may quadruple before a truncated reply is hopeless
TRUNC_TOTAL = 4


class HttpProvider:
    """OpenAI-compatible chat completion, stdlib only, no network at import."""

    def __init__(self, url, model="", key="", timeout=60, temperature=0.2,
                 max_tokens=DEFAULT_MAX_TOKENS, ceiling=TOKENS_CEILING):
        if not url:
            raise ValueError("HttpProvider needs a chat-completions URL")
        self.url = url
        self.model = model
        self.key = key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
        self.ceiling = max(self.max_tokens, int(ceiling or TOKENS_CEILING))

    def __call__(self, job, hint=None):
        """Ask the endpoint; retry only what a retry can fix (v0.15.0).

        Three very different outcomes used to be retried identically: an unreachable
        endpoint, a proxy that answers with an empty message, and a reply cut off by
        the token ceiling -- same prompt, same budget, no pause between them.  The
        kind of the failure now picks the plan: truncated grows the budget once (the
        v0.12.3 behaviour), transport/empty wait and try again, http 4xx and a
        missing configuration stop immediately with the reason still attached.
        """
        prompt = build_prompt(job, hint)
        budget, timeout = self.max_tokens, self.timeout
        attempt = 0
        while True:
            attempt += 1
            out = self._once(prompt, budget, timeout)
            diag = out.get("diag")
            if not isinstance(diag, dict):
                diag = {}
            diag["attempt"] = attempt
            diag["budget"] = budget
            out["diag"] = diag
            kind = str(out.get("kind") or "")
            if kind == ERR_TRUNCATED:
                if budget < self.ceiling and attempt < TRUNC_TOTAL:
                    budget = min(self.ceiling, budget * 4)
                    timeout = min(600, max(timeout, 30) * 3)
                    continue
                return _keep_reason(
                    {"error": _budget_hint(out.get("error") or "", budget),
                     "kind": ERR_TRUNCATED, "diag": diag}, out)
            if kind in RETRYABLE and attempt < RETRY_TOTAL:
                time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
                continue
            return out

    def _diag(self, budget, **extra):
        """What was asked, with what, and what came back -- kept on the job."""
        d = {"provider": "http", "url": self.url, "model": self.model or "",
             "budget": int(budget)}
        d.update(extra)
        return d

    def _fail(self, kind, msg, budget, doc=None, **extra):
        res = {"error": msg, "kind": kind, "diag": self._diag(budget, **extra)}
        if isinstance(doc, dict):
            res["diag"].update(_usage(doc))
            res = _with_reason(res, doc)
            # A reasoning model that spent the whole budget thinking comes back as an
            # empty message with finish_reason=length.  That is the token ceiling, not
            # a proxy hiccup: the fix is a bigger budget (the v0.12.3 behaviour), so the
            # kind has to say 'truncated' even though the text looked 'empty'.
            if res["diag"].get("finish_reason") == "length":
                res["kind"] = ERR_TRUNCATED
                kind = ERR_TRUNCATED
        if kind in FATAL:
            res["fatal"] = True
        return res

    def _once(self, prompt, budget, timeout):
        body = {"messages": [{"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": budget}
        if self.model:
            body["model"] = self.model
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            hint = ""
            if exc.code == 404:
                hint = (" (a 404 usually means the url is the API base, not the full "
                        "chat endpoint -- %s/chat/completions)" % self.url.rstrip("/"))
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                body_text = ""
            res = self._fail(
                ERR_HTTP,
                "model endpoint refused: HTTP %s %s%s%s"
                % (exc.code, exc.reason, hint,
                   (" | " + body_text) if body_text.strip() else ""),
                budget, http_status=exc.code)
            # A wrong key, a forbidden model or a missing route cannot start working
            # because we asked again: fail now and keep the reason in front of the
            # player instead of burying it under two more identical refusals.
            if exc.code in (401, 403, 404):
                res["fatal"] = True
            return res
        except urllib.error.URLError as exc:
            return self._fail(ERR_TRANSPORT, "model endpoint unreachable: %s" % exc, budget)
        except OSError as exc:
            return self._fail(ERR_TRANSPORT, "model call failed: %s" % exc, budget)
        try:
            doc = json.loads(raw)
        except ValueError:
            return self._fail(ERR_BAD_JSON, "model endpoint did not answer JSON", budget,
                              raw_head=raw[:200])
        text = _reply_text(doc)
        if text is None:
            return self._fail(
                ERR_EMPTY,
                "model endpoint answered without a message" + _why_blank(doc, budget),
                budget, doc=doc)
        if not text.strip():
            # An empty content is what a proxy or a token-starved model looks like; the
            # bare 'empty reply' left players guessing, so say what came back instead.
            alt = _alt_text(doc)
            if alt:
                try:
                    parsed = parse_reply(alt)
                except ProviderError:
                    parsed = None
                if parsed is not None:
                    parsed["diag"] = self._diag(budget, reply_chars=len(alt), **_usage(doc))
                    return _with_reason(parsed, doc)
            return self._fail(
                ERR_EMPTY,
                "model endpoint answered with an empty message" + _why_blank(doc, budget),
                budget, doc=doc)
        try:
            parsed = parse_reply(text)
        except ProviderError as exc:
            hint = _why_blank(doc, budget)
            return self._fail(ERR_PARSE, (str(exc) + hint) if hint else str(exc),
                              budget, doc=doc)
        parsed["diag"] = self._diag(budget, reply_chars=len(text), **_usage(doc))
        return _with_reason(parsed, doc)


def _usage(doc):
    """The parts of an OpenAI reply worth keeping as evidence (v0.15.0).

    A job used to record nothing about the call behind it -- no model, no token
    counts, no finish_reason -- so a refusal could not be explained afterwards.
    """
    out = {}
    if not isinstance(doc, dict):
        return out
    choices = doc.get("choices")
    c0 = choices[0] if isinstance(choices, list) and choices else None
    if isinstance(c0, dict):
        fr = c0.get("finish_reason")
        if fr:
            out["finish_reason"] = str(fr)
    usage = doc.get("usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(key)
            if isinstance(v, int):
                out[key] = v
    model = doc.get("model")
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()
    return out

def _budget_hint(err, budget):
    """Say what to change when the ceiling -- not the model -- ended the reply."""
    return (err + " -- the %d-token budget ran out before an answer arrived (a reasoning "
                  "model spends it on reasoning_content first); raise it with "
                  "--llm-max-tokens, or use a model that does not reason" % budget)


def _with_reason(res, doc):
    """Carry the model's own thinking back with the reply (v0.12.4).

    The panel shows it, so a refusal explains itself instead of leaving the player
    with 'ERR model endpoint answered with an empty message'.  'reasoning' is the
    endpoint's reasoning_content (or 'reasoning' on providers that name it that
    way); a model that does not reason simply leaves the key out.
    """
    text = _reason_text(doc)
    if text:
        res["reasoning"] = text[:REASONING_MAX]
    return res


def _keep_reason(res, src):
    """Rewriting a reply's error must not throw away the thinking that came with it."""
    if isinstance(src, dict) and src.get("reasoning"):
        res["reasoning"] = src["reasoning"]
    return res


def _reason_text(doc):
    """The reasoning_content / reasoning text of an OpenAI-style reply, or ''."""
    if isinstance(doc, dict):
        choices = doc.get("choices")
        c0 = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(c0, dict):
            msg = c0.get("message")
            if isinstance(msg, dict):
                for key in ("reasoning_content", "reasoning"):
                    v = msg.get(key)
                    if isinstance(v, str) and v.strip():
                        return v
    return ""


def _alt_text(doc):
    """Some endpoints put the answer in reasoning_content instead of content."""
    if isinstance(doc, dict):
        choices = doc.get("choices")
        c0 = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(c0, dict):
            msg = c0.get("message")
            if isinstance(msg, dict):
                for key in ("reasoning_content", "reasoning"):
                    v = msg.get(key)
                    if isinstance(v, str) and v.strip():
                        return v
    return ""


def _why_blank(doc, budget=None):
    """(finish_reason=length, usage=..., error=...) so a blank reply explains itself.

    v0.12.6: the budget is part of the explanation.  A truncated reply is not a
    refusal -- the model ran out of room mid-thought -- and the job that records it
    may still be retrying, so the message has to say which of the two happened
    without anyone having to read the sidecar's source.
    """
    bits = []
    if budget:
        bits.append("budget=%d tokens" % int(budget))
    if isinstance(doc, dict):
        choices = doc.get("choices")
        c0 = choices[0] if isinstance(choices, list) and choices else None
        if isinstance(c0, dict):
            fr = c0.get("finish_reason")
            if fr:
                bits.append("finish_reason=%s" % fr)
            msg = c0.get("message")
            if isinstance(msg, dict):
                keys = sorted([str(k) for k in msg.keys()])
                if keys:
                    bits.append("message keys=%s" % ",".join(keys))
        usage = doc.get("usage")
        if isinstance(usage, dict) and usage:
            keep = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")
                    if k in usage}
            if keep:
                bits.append("usage=%s" % json.dumps(keep, sort_keys=True))
        if doc.get("error"):
            bits.append("error=%s" % str(doc.get("error"))[:200])
        if isinstance(c0, dict) and c0.get("finish_reason") == "length":
            bits.append("the model ran out of room mid-thought, before it wrote an answer")
    return (" (" + "; ".join(bits) + ")") if bits else ""


def _reply_text(doc):
    """Pull the assistant text out of an OpenAI-style (or plain) reply."""
    if isinstance(doc, dict):
        choices = doc.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0]
            if isinstance(c0, dict):
                msg = c0.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    return msg["content"]
                if isinstance(c0.get("text"), str):
                    return c0["text"]
        if isinstance(doc.get("content"), str):
            return doc["content"]
        if isinstance(doc.get("reply"), str):
            return doc["reply"]
    return None


def provider_from_args(args):
    """Build the provider the CLI asked for, or raise ValueError."""
    kind = getattr(args, "jobs_provider", "none")
    if kind == "template":
        # v0.14.4：模板档从产品里删了，但保留成显式的开发/测试档位。
        import jobqueue
        return jobqueue.template_provider
    if kind == "fake":
        import jobqueue
        return jobqueue.load_fake_provider(args.fake_provider)
    if kind != "llm":
        return None
    inner = None
    if getattr(args, "llm_replay", None):
        inner = load_script(args.llm_replay)
    elif getattr(args, "llm_url", None):
        key = os.environ.get(args.llm_key_env) if getattr(args, "llm_key_env", None) else ""
        inner = HttpProvider(args.llm_url, model=getattr(args, "llm_model", "") or "",
                             key=key or "", timeout=getattr(args, "llm_timeout", 60),
                             max_tokens=getattr(args, "llm_max_tokens", DEFAULT_MAX_TOKENS)
                             or DEFAULT_MAX_TOKENS)
    else:
        raise ValueError("--jobs-provider llm needs --llm-url or --llm-replay")
    if getattr(args, "llm_record", None):
        return Recorder(inner, args.llm_record)
    return inner
