#!/usr/bin/env python3
"""jobqueue -- P4, first cut: the job queue, its state machine, and the write gate.

The mod posts a *job* (one sentence + the region snapshot it was drawn over); a
worker turns that into mlog, runs the same analyzer the gate runs, and only a
clean program ever reaches the 'done' state.  No model is wired in yet: the
generator is a pluggable provider and the built-in one is the template router
that already exists in logicd, so the whole state machine is exercised offline
(docs/Mlog辅助-规划.md section 4.5 asks for exactly that).

Contract (docs/Mlog辅助-规划.md section 4.1)
-------------------------------------------
    {id: sha1(snapshot+desc+mode)[:16], created, state, mode, desc, snapshot,
     attempts, findings[], code, explanation, assumptions[], reasoning,
     workspace,                      # v0.14.0, only for mode='improve'
     sig}                            # v0.14.2, the @aigenerated id (see below)

    queued -> running -> done | rejected | failed | cancelled

The id is content-addressed, so a second POST of the same body is a no-op *while
that job is still in flight* (section 4.2).  Once the job is terminal the same
body starts a NEW run (v0.12.2): because the id is a hash of the request, an
idempotent replay would answer every retry with the old verdict -- a job that
died on 'provider-error: empty reply' would keep answering 'empty reply' after
the endpoint was fixed, and the model would never be asked again.  The retry is
stored as '<id>-2', '<id>-3' ... with 'retry_of' pointing at the first run, so
the failed attempt stays on disk for comparison.

Every transition is written to <dir>/<id>.json before it is reported (the mod
also keeps its own copy, so a sidecar crash does not lose the job).  On startup
a job found in 'running' is put back to 'queued' with its attempt count intact --
that is what "双写可续" means here.  A write can lose a race with whatever else
holds the file for an instant (OneDrive sync, a scanner, a reader), so it is
retried, and a job whose worker still could not be written down ends as 'failed'
rather than sitting in 'running' for ever: from the mod's panel a job that never
moves looks exactly like one still being worked on.

Why link-name-not-bound is fatal here but only a WARN in the analyzer
--------------------------------------------------------------------
mlogcheck reports a link-shaped name the processor does not own as WARN: a human
may be halfway through wiring it up and still wants to see the rest.  A job is
machine output written straight into a processor, so an unbound link name is a
broken write, not a hint.  FATAL_RIDS promotes exactly that rule to a refusal,
and it is the one case docs/Mlog辅助-规划.md section 4.5 names as an acceptance
test.

Why 'reasoning' is part of the job (v0.12.4)
--------------------------------------------
A model that reasons spends most of its budget on reasoning_content before it
writes the program, and when it runs out of budget -- or simply reasons itself
into a corner -- the player sees only 'rejected' with no way to tell why.  So the
thinking is kept on the job ('reasoning', the latest attempt's text) and the mod
shows it in the panel: the answer to "why did it write that" is in there, and
without it a refusal is indistinguishable from a model that ignored the request.

Why the code is signed before it is gated (v0.14.2)
---------------------------------------------------
The mod writes `job['code']` into the processor and then reads `b.code` back to
check it got there (main.js armWriteCheck compares the two character by
character).  A signature added after the job was stored would therefore read as
"no permission"; adding it before means the job's code IS the program, and the
`@aigenerated` token rides along invisibly (a `jump ... always ...` compare slot
that `always` never reads, see aigenerated.py).  The gate and the compiler run on
the signed text, so the findings describe exactly what gets written, and a model
that hands back the previous version of an 'improve' job is re-signed with
force=True instead of tripping over the old token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import traceback

import aigenerated               # noqa: E402  (tools/ is on sys.path next to this file)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: one first try plus two self-repairs (docs/Mlog辅助-规划.md:175)
MAX_ATTEMPTS = 3

#: how many runs of one content id are numbered before the id gets a random tail
#: (a player would have to re-send the same sentence 98 times to get there)
RETRY_MAX = 99

#: a job file is written tmp + os.replace; on Windows the replace can lose a race
#: with anything else holding the file for a moment (OneDrive sync, a virus
#: scanner, another reader), which shows up as WinError 5 / 32 / 33.  A save that
#: died there used to leave the job stuck in 'running' with nothing in the panel,
#: so the write is now retried instead of giving up on the first collision.
SAVE_TRIES = 6
SAVE_WAIT = 0.03

#: how much of a model's reasoning is kept on the job (the panel shows the head
#: of it; a reasoning model can spend thousands of tokens before it writes a line)
REASONING_MAX = 20000

#: v0.14.0: how much of a workspace ("continue improving" context) a job carries.
#: The prompt only ever shows the first ~1200 characters of prevCode
#: (mlogprovider.previous_version), so these caps are about what a job file and a
#: GET /jobs?id= can afford to hold -- not about what the model gets to see.
WS_NAME_MAX = 120
WS_NOTES_MAX = 500
WS_CODE_MAX = 4000
WS_JOB_MAX = 64
WS_FINDINGS_MAX = 12
WS_FINDING_MAX = 300
WS_TRUNC = '\n... (truncated)'

_WIN_CONTENDED = (5, 32, 33)


def _contended(exc):
    """True when an OSError only means 'someone else had the file open just now'."""
    return isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in _WIN_CONTENDED


def _replace(src, dst):
    """os.replace behind a name of its own, so a test can make it collide on purpose."""
    os.replace(src, dst)


STATES = ('queued', 'running', 'done', 'rejected', 'failed', 'cancelled')
TERMINAL = ('done', 'rejected', 'failed', 'cancelled')

#: analyzer WARNs that a machine write must not accept (see the module docstring)
FATAL_RIDS = ('link-name-not-bound',)

#: the author stamped on every program this queue writes into a processor.
#: The hidden `@aigenerated` signature rides along in the code the mod writes,
#: so the job's `code` and the processor's `b.code` stay byte-identical
#: (armWriteCheck compares them) while the attribution travels with the program.
SIGN_AUTHOR = aigenerated.SIGN_AUTHOR

DEFAULT_DIR = os.path.join(ROOT, 'out', 'jobs')


def _logicd():
    """Late import: logicd imports this module at load time, so the edge is one-way."""
    import logicd
    return logicd


def default_dir():
    return os.environ.get('MLOG_JOBS_DIR') or DEFAULT_DIR


def job_id(snapshot, desc, mode, prev_code=None):
    """sha1(snapshot + desc + mode)[:16] -- stable across retries, so POST is idempotent.

    v0.14.0: an 'improve' job also hashes the code it starts from.  "Continue
    improving" on the same sentence with a *different* previous version is the
    normal second round, and without this it hashed to the same id as the first
    round -- so the queue answered the second request with the first run (or a
    '-2' replay of it) and the model was never asked about the new code.  The
    extra key is added only for 'improve': a 'new'/'edit' id has to stay exactly
    what it was, or every job id the mod already knows changes under it.
    """
    blob = {'snapshot': snapshot, 'desc': desc, 'mode': mode}
    if mode == 'improve':
        blob['prev'] = prev_code or ''
    body = json.dumps(blob, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(body.encode('utf-8')).hexdigest()[:16]


def _ws_text(ws, key, limit):
    """One string field of a workspace, truncated to `limit`; junk becomes ''."""
    v = ws.get(key)
    if not isinstance(v, str):
        return ''
    return v if len(v) <= limit else v[:limit]


def _ws_code(ws):
    """prevCode: the head of the last version that was actually written (v0.14.0).

    The tail is usually where a broken program went wrong, but the head is what
    the next attempt has to keep working, so the head is what is kept.  The
    marker counts against the cap, so the stored field never exceeds it.
    """
    v = ws.get('prevCode')
    if not isinstance(v, str):
        return ''
    if len(v) <= WS_CODE_MAX:
        return v
    return v[:WS_CODE_MAX - len(WS_TRUNC)] + WS_TRUNC


def _ws_finding(f):
    """One readable line per previous finding (v0.14.1).

    The mod sends {sev, rid, msg} objects; str(dict) would leak Python repr into
    the prompt ("{'sev': 'WARN', ...}"), which tells the model nothing.
    """
    if isinstance(f, str):
        return f[:WS_FINDING_MAX]
    if isinstance(f, dict):
        sev = str(f.get('sev') or '').strip()
        rid = str(f.get('rid') or '').strip()
        msg = str(f.get('msg') or '').strip()
        head = (sev + ' ' + rid).strip()
        line = (head + ': ' + msg) if msg else head
        return line[:WS_FINDING_MAX]
    if f is None:
        return ''
    return str(f)[:WS_FINDING_MAX]


def workspace_of(body):
    """The 'continue improving' context of a request, or None (v0.14.0).

    Missing or not an object is None, never an error: the job body is a contract
    the mod owns, and a mod that knows nothing about workspaces must keep posting
    jobs.  Every field is optional and junk is dropped rather than refused for
    the same reason -- a half-filled workspace still tells the model something.
    """
    ws = body.get('workspace')
    if not isinstance(ws, dict):
        return None
    findings = ws.get('prevFindings')
    if not isinstance(findings, list):
        findings = []
    rows = []
    for f in findings[:WS_FINDINGS_MAX]:
        line = _ws_finding(f)
        if line:
            rows.append(line)
    return {'id': _ws_text(ws, 'id', WS_NAME_MAX),
            'name': _ws_text(ws, 'name', WS_NAME_MAX),
            'notes': _ws_text(ws, 'notes', WS_NOTES_MAX),
            'prevCode': _ws_code(ws),
            'prevJob': _ws_text(ws, 'prevJob', WS_JOB_MAX),
            'prevFindings': rows}


def _int(v, name, default=None):
    if v is None and default is not None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError('%s must be an integer' % name)
    return int(v)


def normalize(body):
    """A request body -> the job object of section 4.1 (raises ValueError on junk)."""
    if not isinstance(body, dict):
        raise ValueError('job must be a JSON object')
    desc = body.get('desc')
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError('job.desc must be a non-empty string')
    desc = desc.strip()
    if len(desc) > 2000:
        raise ValueError('job.desc is too long (2000 characters)')
    mode = body.get('mode') or 'new'
    if mode not in ('new', 'edit', 'improve'):
        raise ValueError("job.mode must be 'new', 'edit' or 'improve'")
    snap = body.get('snapshot')
    if snap is None:
        snap = {}
    if not isinstance(snap, dict):
        raise ValueError('job.snapshot must be an object (main.js: maiSnapshot(x1,y1,x2,y2))')
    # null means "the mod could not read the link table" (unknown); [] means the
    # table is known and empty, which is what the gate needs to catch a
    # hallucinated link name on a processor that links to nothing.
    links = body.get('links')
    if links is not None and not isinstance(links, list):
        raise ValueError('job.links must be a list, or null when the link table is unknown')
    origin = body.get('origin')
    if origin is not None:
        if not isinstance(origin, dict):
            raise ValueError('job.origin must be an object with x and y')
        origin = {'x': _int(origin.get('x'), 'origin.x', 0),
                  'y': _int(origin.get('y'), 'origin.y', 0)}
    ipt = _int(body.get('ipt'), 'job.ipt', 2)
    if not 1 <= ipt <= 40:
        raise ValueError('job.ipt must be 1..40 (LogicBlock.maxInstructionsPerTick)')
    # v0.15.1: which processor of a batch this is.  It is NOT part of the id --
    # two processors in one box hash the same on purpose, and the queue already
    # keeps them apart with the -2/-3 suffixes.  Here it only tells the model that
    # the same sentence went to N processors with N different link tables.
    batch = body.get('batch')
    if batch is not None:
        if not isinstance(batch, dict):
            raise ValueError('job.batch must be an object with i and n')
        bi = _int(batch.get('i'), 'batch.i', 1)
        bn = _int(batch.get('n'), 'batch.n', 1)
        bn = max(1, bn)
        batch = {'i': min(max(1, bi), bn), 'n': bn}
    # v0.14.0: parsed before the id, because an 'improve' id hashes prevCode
    ws = workspace_of(body)
    jid = job_id(snap, desc, mode, ws.get('prevCode') if ws else None)
    return {'id': jid, 'created': int(body.get('created') or time.time() * 1000),
            'state': 'queued', 'mode': mode, 'desc': desc, 'snapshot': snap,
            'links': links, 'origin': origin, 'ipt': ipt, 'workspace': ws,
            'batch': batch,
            'attempts': 0, 'findings': [], 'code': '', 'explanation': '',
            'assumptions': [], 'template': None, 'params': {}, 'bytes_b64': ''}


def gate(code, links, ipt=2):
    """Analyze generated code and promote FATAL_RIDS to refusals.

    links=[] is authoritative (the processor has no links), links=None is unknown
    and skips the link check entirely -- logicd.analyse() keeps that distinction.

    Returns the same shape logicd.analyse() returns, plus 'fatal' -- the promoted
    findings, so the mod can tell "the model wrote a link it does not own" apart
    from "the model wrote an unrelated ERR".
    """
    res = _logicd().analyse(code, links, ipt=ipt)
    fatal = []
    for f in res['findings']:
        if f.get('rid') in FATAL_RIDS:
            f['sev'] = 'ERR'
            f['fatal'] = True
            fatal.append(f)
    if fatal:
        res['errors'] += len(fatal)
        res['ok'] = False
    res['fatal'] = fatal
    return res


class Store:
    """One JSON file per job; every write is tmp + os.replace so a crash never truncates."""

    def __init__(self, path=None):
        self.path = path or default_dir()
        os.makedirs(self.path, exist_ok=True)

    def file(self, jid):
        return os.path.join(self.path, jid + '.json')

    def save(self, job):
        body = json.dumps(job, ensure_ascii=False, indent=1, sort_keys=True)
        dest = self.file(job['id'])
        tmp = '%s.%d.%d.tmp' % (dest, os.getpid(), threading.get_ident())
        last = None
        for n in range(SAVE_TRIES):
            try:
                with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                _replace(tmp, dest)
                return dest
            except OSError as exc:            # a watcher held the file for a moment
                if not _contended(exc):
                    raise
                last = exc
                time.sleep(SAVE_WAIT * (n + 1))
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise last

    def load(self, jid):
        path = self.file(jid)
        if not os.path.isfile(path):
            return None
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)

    def ids(self):
        return sorted(n[:-5] for n in os.listdir(self.path) if n.endswith('.json'))

    def all(self):
        out = []
        for jid in self.ids():
            try:
                job = self.load(jid)
            except Exception:
                continue                      # a half-written file is not a job
            if isinstance(job, dict) and job.get('id'):
                out.append(job)
        return out



#: v0.15.0: the provider already retried what a retry can fix (a transport hiccup,
#: an empty completion).  Re-running the whole job for the same class only
#: multiplies the wait -- the previous build sent the identical prompt three times
#: and reported 'refused after 3 attempt(s)' without naming the wall.
PROVIDER_EXHAUSTED = ('empty', 'transport')
#: a job is a small file the mod reads over HTTP; keep the evidence bounded
ATTEMPT_LOG_MAX = 8


def _exhausted_text(kind, err, attempts):
    """Say which wall it was, not just that the job gave up (v0.15.0)."""
    if kind == 'empty':
        return ('the endpoint returned an empty message %d times in a row -- the '
                'proxy or the model produced no content. Check the key and the '
                'proxy, or raise --llm-max-tokens.' % attempts)
    if kind == 'transport':
        return ('the endpoint could not be reached %d times in a row (%s). Check the '
                'address, the proxy, and that this machine can reach it.'
                % (attempts, str(err)[:160]))
    return str(err)


class Queue:
    """The state machine.  provider(job, hint) -> output dict is the only seam to a model."""

    def __init__(self, store=None, provider=None, poll=0.05, workers=1):
        self.store = store or Store()
        # v0.14.4：默认不再是模板档 —— 没配模型就明确说没配。
        self.provider = provider or unconfigured_provider
        self.poll = poll
        self.lock = threading.RLock()
        self.jobs = {}
        # v0.15.0: a selection of N processors is N model calls; one worker made
        # that N calls in a row while the player watched the same badge.
        self.workers = max(1, int(workers or 1))
        self.threads = []
        self.running = False
        self.resumed = 0
        for job in self.store.all():
            if job.get('state') in ('running', 'claimed'):
                job['state'] = 'queued'           # crashed mid-flight: put it back
                job['resumed'] = True
                self.store.save(job)
                self.resumed += 1
            elif job.get('state') == 'queued':
                self.resumed += 1
            self.jobs[job['id']] = job

    # -- public API ------------------------------------------------------
    def _free_id(self, base):
        """(id, None) for an id nobody holds, (id, job) when that id is in flight.

        A *terminal* job does not hold its content id (v0.12.2): the id is
        content-addressed, so treating a finished job as "already done" turns
        every retry of the same sentence into a replay of the old verdict -- a
        job rejected on 'provider-error: empty reply' kept answering 'empty
        reply' forever, which reads exactly like "the fix did not work".  A
        finished run therefore moves the retry to base-2, base-3, ... while a
        queued/running one still dedupes (section 4.2).
        """
        jid = base
        for n in range(2, RETRY_MAX + 1):
            old = self.jobs.get(jid)
            if old is None:
                return jid, None
            if old.get('state') not in TERMINAL:
                return jid, old
            jid = '%s-%d' % (base, n)
        return '%s-%s' % (base, os.urandom(3).hex()), None

    def enqueue(self, body):
        """-> (job, created).

        A second POST of the same body is a no-op while the job is still
        queued/running (section 4.2); after it finished, the same body starts a
        new run -- see _free_id for why the replay had to go.
        """
        job = normalize(body)
        base = job['id']
        with self.lock:
            jid, old = self._free_id(base)
            if old is not None:
                return dict(old), False
            if jid != base:
                job['id'] = jid
                job['retry_of'] = base
            self.jobs[jid] = job
            self.store.save(job)
            return dict(job), True

    def get(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            return dict(job) if job else None

    def list(self, state=None):
        with self.lock:
            jobs = [dict(j) for j in self.jobs.values()]
        if state:
            jobs = [j for j in jobs if j.get('state') == state]
        else:
            jobs = [j for j in jobs if j.get('state') not in TERMINAL]
        return sorted(jobs, key=lambda j: (j.get('created') or 0, j.get('id') or ''))

    def history(self, limit=50, state=None):
        """Every job the store knows, newest first -- finished ones included.

        list() answers "what is in flight"; the job manager window in the mod
        wants the opposite ("what happened, and how did it end"), so this one
        keeps the terminal runs and sorts them newest first: a player looking
        for the run they just started scrolls the top, not the bottom.
        """
        with self.lock:
            jobs = [dict(j) for j in self.jobs.values()]
        if state:
            jobs = [j for j in jobs if j.get('state') == state]
        jobs.sort(key=lambda j: (j.get('created') or 0, j.get('id') or ''), reverse=True)
        if limit is not None and limit > 0:
            jobs = jobs[:limit]
        return jobs

    def cancel(self, jid):
        """Cancelling is allowed in every non-terminal state (section 4.2)."""
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return None
            if job['state'] in TERMINAL:
                return dict(job)
            job['cancel_requested'] = True
            if job['state'] == 'queued':
                job['state'] = 'cancelled'
                job['finished'] = int(time.time() * 1000)
            self.store.save(job)
            return dict(job)

    def start(self):
        if self.threads:
            return self
        self.running = True
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name='jobqueue-%d' % (i + 1),
                                 daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self):
        self.running = False
        threads = self.threads
        self.threads = []
        for t in threads:
            t.join(timeout=5)
        return self

    def drain(self, timeout=20.0):
        """Run the worker inline until nothing is queued (tests, one-shot CLI)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            jid = self._claim()
            if jid is None:
                return True
            self._run(jid)
        return False

    # -- worker ----------------------------------------------------------
    def _claim(self):
        """Atomically take the oldest queued job (v0.15.0).

        _next() only *looked* at the queue; with more than one worker two threads
        could look, see the same job and run it twice.  Claiming marks the job in the
        same critical section that found it.
        """
        with self.lock:
            for jid in sorted(self.jobs, key=lambda i: (self.jobs[i].get('created') or 0, i)):
                job = self.jobs[jid]
                if job['state'] == 'queued':
                    job['state'] = 'claimed'
                    self.store.save(job)
                    return jid
        return None

    def _loop(self):
        while self.running:
            jid = self._claim()
            if jid is None:
                time.sleep(self.poll)
                continue
            try:
                self._run(jid)
            except Exception as exc:          # the worker must outlive one bad job
                traceback.print_exc()
                self._strand(jid, exc)

    def _strand(self, jid, exc):
        """A job whose worker died must end up terminal, not 'running' for ever.

        Every failure inside _run is supposed to turn into 'failed' by itself, but
        a write that keeps colliding (see SAVE_TRIES) escapes that, and the panel
        then shows a job that never moves -- indistinguishable, for the player,
        from work still in progress.  Best effort: if the disk is still refusing
        writes there is nothing left to persist the verdict to.
        """
        text = '%s: %s' % (type(exc).__name__, exc)
        with self.lock:
            job = self.jobs.get(jid)
            if job is None or job.get('state') in TERMINAL:
                return
        try:
            self._set(jid, state='failed', finished=int(time.time() * 1000),
                      findings=[{'sev': 'ERR', 'rid': 'worker-error', 'line': 0, 'msg': text[:300]}],
                      explanation='the worker could not finish this job: ' + text[:200])
        except Exception:
            traceback.print_exc()

    def _record_attempt(self, jid, diag, kind, err, started):
        """Keep what each ask of the model produced and how it ended (v0.15.0).

        The job used to carry the verdict only, so a refusal could not be explained
        afterwards: no model name, no token counts, no finish_reason, no duration.
        """
        rec = {'n': int(self.jobs.get(jid, {}).get('attempts') or 0),
               'ms': int((time.time() - started) * 1000)}
        for key in ('provider', 'url', 'model', 'budget', 'finish_reason',
                    'prompt_tokens', 'completion_tokens', 'total_tokens',
                    'reply_chars', 'attempt', 'http_status'):
            if diag.get(key) not in (None, ''):
                rec[key] = diag.get(key)
        if kind:
            rec['kind'] = kind
        if err:
            rec['error'] = str(err)[:300]
        with self.lock:
            job = self.jobs.get(jid)
            if job is None:
                return
            log = job.get('attempt_log')
            if not isinstance(log, list):
                log = []
            log.append(rec)
            job['attempt_log'] = log[-ATTEMPT_LOG_MAX:]
            if diag:
                job['diag'] = dict(diag)
                if diag.get('model'):
                    job['model'] = diag['model']
                if diag.get('provider'):
                    job['provider'] = diag['provider']
            self.store.save(job)

    def _set(self, jid, **fields):
        with self.lock:
            job = self.jobs[jid]
            job.update(fields)
            self.store.save(job)
            return job

    def _cancelled(self, job):
        if job.get('cancel_requested'):
            self._set(job['id'], state='cancelled', finished=int(time.time() * 1000))
            return True
        return False

    def _run(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if job is None or job['state'] not in ('queued', 'claimed'):
                return
            job['state'] = 'running'
            job['started'] = int(time.time() * 1000)
            job.pop('resumed', None)
            self.store.save(job)
        hint = None
        while True:
            if self._cancelled(job):
                return
            if job['attempts'] >= MAX_ATTEMPTS:
                self._set(jid, state='rejected', finished=int(time.time() * 1000),
                          explanation=job.get('explanation') or
                          'refused after %d attempt(s): the gate never saw a clean program'
                          % MAX_ATTEMPTS)
                return
            job['attempts'] += 1
            with self.lock:
                self.store.save(job)
            started = time.time()
            try:
                out = self.provider(dict(job), hint)
            except Exception as exc:
                self._set(jid, state='failed', finished=int(time.time() * 1000),
                          explanation='provider raised %s: %s' % (type(exc).__name__, exc))
                return
            if self._cancelled(job):
                return
            if not isinstance(out, dict):
                out = {'error': 'provider returned %r, want an object' % (out,)}
            # keep the model's own thinking on the job, before the error branch: a
            # refusal is exactly when the player wants to read what it was thinking.
            think = out.get('reasoning')
            if isinstance(think, str) and think.strip():
                self._set(jid, reasoning=think[:REASONING_MAX])
            diag = out.get('diag') if isinstance(out.get('diag'), dict) else {}
            kind = str(out.get('kind') or '')
            self._record_attempt(jid, diag, kind, out.get('error'), started)
            if out.get('error'):
                finding = {'sev': 'ERR', 'rid': 'provider-error', 'line': 0,
                           'msg': str(out['error'])}
                if kind:
                    finding['kind'] = kind
                hint = [finding]
                self._set(jid, findings=hint)
                if out.get('fatal'):
                    # 重试只会得到同一句话（例如「未配置模型」），直接失败收场。
                    self._set(jid, state='failed', finished=int(time.time() * 1000),
                              explanation=str(out['error']))
                    return
                if kind in PROVIDER_EXHAUSTED and job['attempts'] >= 2:
                    self._set(jid, state='failed', finished=int(time.time() * 1000),
                              explanation=_exhausted_text(kind, out['error'],
                                                          job['attempts']))
                    return
                continue
            res = self._materialize(job, out, jid)
            self._set(jid, findings=res['findings'], explanation=res.get('explanation') or '',
                      assumptions=res.get('assumptions') or [],
                      cost=res.get('cost') or '',
                      template=res.get('template'), params=res.get('params') or {})
            if res['ok']:
                self._set(jid, state='done', code=res['code'],
                          sig=res.get('sig') or '',
                          bytes_b64=res.get('bytes_b64') or '',
                          statements=res.get('statements') or 0,
                          finished=int(time.time() * 1000))
                return
            hint = res['findings']

    def _materialize(self, job, out, job_id=''):
        """Provider output -> {ok, code, findings, ...}; the anchored tier goes through compose."""
        # 签名里的 meta 要带作业 id，_run 里那个 jid 才是权威值
        job = dict(job, id=job_id or job.get('id') or '')
        L = _logicd()
        # keep None ("the mod could not read the table") apart from [] ("no links"):
        # the gate treats the empty list as authoritative and skips None entirely.
        links = job.get('links')
        ipt = job.get('ipt') or 2
        if isinstance(out.get('code'), str) and out['code'].strip():
            code = out['code']
            # sign first (v0.14.2): the mod writes job['code'] and then reads
            # b.code back, so the signed text -- not the raw reply -- has to be
            # what the job holds, what the gate reads and what gets compiled.
            # force=True: an 'improve' reply often still carries the previous
            # version's token, which must be replaced, not rejected.
            try:
                code, sig = aigenerated.sign_text(
                    code, SIGN_AUTHOR, force=True,
                    meta=aigenerated.meta_of_job(job))
            except aigenerated.SignError as exc:
                res = gate(code, links, ipt=ipt)
                res['findings'] = [{'sev': 'ERR', 'rid': 'sign-error', 'line': 0,
                                    'msg': 'the reply could not be signed: %s' % exc}]
                res['ok'] = False
                res['errors'] = 1
                res['code'] = ''
                return res
            res = gate(code, links, ipt=ipt)
            # the reply may also DECLARE which links it wants (section 4.3 needs_links);
            # a name that is not in an authoritative table is a refusal, not a warning.
            if links is not None:
                have = L._link_names(links)
                missing = [str(n) for n in (out.get('needs_links') or []) if str(n) not in have]
                if missing:
                    bad = {'sev': 'ERR', 'rid': 'link-name-not-bound', 'line': 0,
                           'fatal': True,
                           'msg': 'the reply needs link(s) this processor does not have: '
                                  + ', '.join(missing)}
                    res['findings'].append(bad)
                    res['fatal'].append(bad)
                    res['errors'] += 1
                    res['ok'] = False
            payload = L.msch.build_logic_data(code, L._relative_links(links or [], job.get('origin')))
            res.update({'code': code,
                        'sig': sig,
                        'bytes_b64': base64.b64encode(payload).decode('ascii'),
                        'explanation': out.get('explanation') or '',
                        'assumptions': out.get('assumptions') or []})
            return res
        tid = out.get('template')
        if not tid:
            res = gate('', links, ipt=ipt)
            res['findings'] = [{'sev': 'ERR', 'rid': 'provider-no-code', 'line': 0,
                                'msg': 'provider returned neither code nor template'}]
            res['ok'] = False
            res['code'] = ''
            return res
        cres = L.compose({'template': tid, 'params': out.get('params') or {},
                          'links': links, 'ipt': ipt, 'origin': job.get('origin')})
        # compose() reports analyzer severities; the job gate is stricter
        fatal = []
        for f in cres['findings']:
            if f.get('rid') in FATAL_RIDS and f['sev'] != 'ERR':
                f['sev'] = 'ERR'
                f['fatal'] = True
                fatal.append(f)
        if fatal:
            cres['errors'] += len(fatal)
            cres['ok'] = False
        cres['fatal'] = fatal
        cres['explanation'] = out.get('explanation') or ''
        cres['assumptions'] = out.get('assumptions') or []
        cres['template'] = tid
        cres['params'] = out.get('params') or {}
        return cres


# ---------------------------------------------------------------- providers

def unconfigured_provider(job, hint=None):
    """The product default since v0.14.4: no model configured.

    The template tier is gone from the product (the player asked for it), so a job
    that reaches the queue without a model must say so and stop -- not quietly
    stamp out a canned program nobody asked for.  ``fatal`` tells the retry loop
    that a second attempt would fail exactly the same way.
    """
    return {'error': '未配置模型：请在 设置 -> Mlog辅助 -> 大模型 里填地址、密钥、模型，'
                     '再点「启用模型档」',
            'kind': 'config',
            'fatal': True}


def template_provider(job, hint=None):
    """The offline tier: the intent router already in logicd picks a template."""
    L = _logicd()
    res = L.plan({'intent': job['desc'], 'links': job.get('links') or [],
                  'ipt': job.get('ipt') or 2, 'origin': job.get('origin')})
    if not res.get('template'):
        why = res.get('reason') or 'no template matched this description'
        return {'error': why + ' | 当前是模板档，只能套固定样板；要自由描述，请在 设置 -> '
                                'Mlog辅助 -> 大模型 里填地址/密钥/模型，再点「启用模型档」'}
    return {'template': res['template'], 'params': res.get('params') or {},
            'explanation': res.get('reason') or ('template %s' % res['template']),
            'assumptions': []}


class FakeProvider:
    """Test-only provider: answers from a table keyed by a substring of the description.

    A value may be a list, consumed one entry per attempt -- that is how a
    self-repair case is written (first a hallucinated link, then good code).  A
    value may carry 'sleep' seconds so a cancellation test has a window.
    """

    def __init__(self, table, default=None):
        self.table = table or {}
        self.default = default if default is not None else {'error': 'no fake response'}
        self.calls = {}

    def _pick(self, job, hint):
        # longest key wins, so a table can hold both a short and a longer phrase
        key = None
        for k in self.table:
            if k and k in job['desc'] and (key is None or len(k) > len(key)):
                key = k
        if key is None:
            return self.default
        val = self.table[key]
        if isinstance(val, list):
            n = self.calls.get(key, 0)
            self.calls[key] = n + 1
            return val[min(n, len(val) - 1)]
        return val

    def __call__(self, job, hint=None):
        out = self._pick(job, hint)
        if isinstance(out, dict) and out.get('sleep'):
            time.sleep(float(out['sleep']))
        return out


def load_fake_provider(path):
    with open(path, encoding='utf-8') as fh:
        table = json.load(fh)
    if not isinstance(table, dict):
        raise ValueError('fake provider file must be a JSON object')
    default = table.pop('__default__', None)
    return FakeProvider(table, default)
