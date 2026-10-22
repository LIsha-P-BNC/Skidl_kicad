"""Shared team AI design-skill store on S3.

One company-wide JSON file accumulates design skills USERS stated in the
Anvil CAD AI chat (circuit approaches, part choices, placement and routing
preferences). Every machine PULLS it on app launch (anonymous GET, done by
the C++ shell into the local cache this module also maintains); the AI
PUSHES newly extracted skills from here.

Contract (mirrors anvil_publish.py):

    1. USER-ORIGIN GATE: a skill may only be saved with a verbatim `quote`
       that actually appears in the user's own chat messages. The C++ chat
       panel appends every outgoing user message to user_messages.log in
       $ANVIL_SKILLS_DIR; quote_ok() checks the quote against that log, so
       the AI structurally cannot store its own suggestions.
    2. Dedupe BEFORE push: fetch the live skills.json; an equivalent skill
       already on the server (same normalized-text sha256, or fuzzy match in
       the same category) is NOT pushed again -- only its evidence_count is
       bumped.
    3. Push = conditional PUT (boto3 IfMatch=<etag>) so two users saving at
       once cannot clobber each other; a 412 re-fetches, re-merges, retries.
       No AWS credentials / offline -> the delta is queued to an outbox and
       drained on a later server start. Failures NEVER raise into the tool.

INTERIM AUTH NOTE: like anvil_publish.py, uploads use the machine's AWS
profile until the Envil backend offers presigned PUTs (login JWT, per-user
audit). The READ path is anonymous and final (public-read prefix).

Environment (set by the C++ agent via anvil_mcp.json):
    ANVIL_SKILLS_DIR        local dir: skills.json cache, user_messages.log,
                            outbox\\  (default: %APPDATA%\\anvil\\1.0\\anvil_ai_skills)
    ANVIL_USER_EMAIL        author recorded on new skills
Optional overrides:
    ANVIL_SKILLS_BUCKET     default dms-booma
    ANVIL_SKILLS_KEY        default anvilcad_electronics/anvil_ai_skills/skills.json
    ANVIL_SKILLS_REGION     default ap-south-1
    ANVIL_SKILLS_AWS_PROFILE default anvil (ignored if absent)
    ANVIL_SKILLS_PUSH       "0" disables remote push entirely (local cache only)
"""

import hashlib
import json
import os
import re
import time
import uuid

BUCKET = os.environ.get("ANVIL_SKILLS_BUCKET", "dms-booma")
KEY = os.environ.get("ANVIL_SKILLS_KEY",
                     "anvilcad_electronics/anvil_ai_skills/skills.json")
REGION = os.environ.get("ANVIL_SKILLS_REGION", "ap-south-1")
PROFILE = os.environ.get("ANVIL_SKILLS_AWS_PROFILE", "anvil")
PUSH_ENABLED = os.environ.get("ANVIL_SKILLS_PUSH", "1") != "0"

HTTP_BASE = "https://%s.s3.%s.amazonaws.com" % (BUCKET, REGION)
HTTP_TIMEOUT = 15          # seconds; a slow link must never stall a chat turn

CATEGORIES = {"circuit", "parts", "placement", "routing", "power",
              "workflow", "other"}
MAX_ACTIVE = 200           # active (non-archived, non-deleted) skills cap
CONFIRM_AT = 2             # evidence_count that promotes candidate->confirmed
FUZZY_OVERLAP = 0.8        # token-overlap ratio treated as "same skill"

# Obvious credential-shaped statements must never enter a shared file.
_SECRET_RE = re.compile(
    r"(password|passwd|api[\s_-]?key|secret|bearer|credential|otp\s*(code|is)"
    r"|aws_access|private key)", re.I)


# --------------------------------------------------------------------------
# local layout
# --------------------------------------------------------------------------

def _dir():
    d = os.environ.get("ANVIL_SKILLS_DIR")
    if not d:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                         "anvil", "1.0", "anvil_ai_skills")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path():
    return os.path.join(_dir(), "skills.json")


def _log_path():
    return os.path.join(_dir(), "user_messages.log")


def _outbox():
    d = os.path.join(_dir(), "outbox")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(text):
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _text_sha(text):
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def skeleton():
    return {"schema_version": 1, "rev": 0, "updated_at": _now(), "skills": []}


def _valid(doc):
    return (isinstance(doc, dict) and isinstance(doc.get("schema_version"), int)
            and isinstance(doc.get("skills"), list))


def _active(doc):
    return [s for s in doc.get("skills", [])
            if not s.get("deleted") and s.get("status") != "archived"]


# --------------------------------------------------------------------------
# local cache (what the C++ prompt builder reads)
# --------------------------------------------------------------------------

def write_local_cache(doc):
    """Atomic replace so the C++ reader never sees a torn file."""
    try:
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, _cache_path())
    except OSError:
        pass                      # cache trouble never blocks a save


def load_local_cache():
    try:
        with open(_cache_path(), "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
        if _valid(doc):
            return doc
    except Exception:  # noqa: BLE001 - corrupt/missing cache -> fresh skeleton
        pass
    return skeleton()


# --------------------------------------------------------------------------
# user-origin gate
# --------------------------------------------------------------------------

def quote_ok(quote):
    """(ok, reason). The quote must be the user's own words: a normalized
    substring of the user-message log the C++ chat panel maintains."""
    q = _norm(quote)
    if len(q) < 8:
        return False, "quote too short -- give the user's own verbatim words"
    if _SECRET_RE.search(q):
        return False, "do not store credentials or secrets in team memory"
    try:
        size = os.path.getsize(_log_path())
        with open(_log_path(), "rb") as f:
            if size > 256 * 1024:
                f.seek(size - 256 * 1024)
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False, ("no user-message log found -- skills can only be saved "
                       "from the in-app Anvil CAD chat")
    if q in _norm(tail):
        return True, ""
    return False, ("quote not found in the user's own messages -- only save "
                   "skills the USER stated verbatim, never your own "
                   "suggestions or rephrasings")


# --------------------------------------------------------------------------
# S3 fetch / conditional push
# --------------------------------------------------------------------------

def fetch():
    """(doc, etag) from the live S3 object. Missing object -> fresh skeleton
    with etag None (first PUT then uses If-None-Match). Unreachable/corrupt
    -> (None, None): caller falls back to the local cache + outbox."""
    try:
        import requests
        r = requests.get("%s/%s" % (HTTP_BASE, KEY), timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "Anvil-Skills/1.0"})
        if r.status_code in (403, 404):
            return skeleton(), None
        if r.status_code != 200:
            return None, None
        doc = json.loads(r.content.decode("utf-8-sig"))
        if not _valid(doc):
            return None, None
        return doc, r.headers.get("ETag", "").strip('"') or None
    except Exception:  # noqa: BLE001 - offline must never raise into a turn
        return None, None


class _Conflict(Exception):
    """Another writer changed the object between our GET and PUT."""


def _put(doc, etag):
    """Conditional PUT via boto3 (bundled). Raises _Conflict on a lost race;
    any other failure raises its original exception (caller queues outbox)."""
    import boto3
    import botocore.exceptions
    try:
        session = boto3.Session(profile_name=PROFILE)
    except Exception:  # noqa: BLE001 - profile absent -> default credential chain
        session = boto3.Session()
    client = session.client("s3", region_name=REGION)
    body = json.dumps(doc, indent=1).encode("utf-8")
    kwargs = dict(Bucket=BUCKET, Key=KEY, Body=body,
                  ContentType="application/json", CacheControl="max-age=30")
    if etag:
        kwargs["IfMatch"] = etag
    else:
        kwargs["IfNoneMatch"] = "*"
    try:
        client.put_object(**kwargs)
    except botocore.exceptions.ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code in ("PreconditionFailed", "ConditionalRequestConflict") \
                or status in (409, 412):
            raise _Conflict() from e
        raise


def _queue_outbox(entries):
    """Persist skill entries that could not reach S3; drained later."""
    try:
        name = "%d-%s.json" % (int(time.time()), uuid.uuid4().hex[:8])
        tmp = os.path.join(_outbox(), name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"skills": entries}, f)
        os.replace(tmp, os.path.join(_outbox(), name))
    except OSError:
        pass


# --------------------------------------------------------------------------
# merge / dedup
# --------------------------------------------------------------------------

def _tokens(text):
    """Normalized word set with cheap plural stemming, so 'packages' restates
    'package' instead of reading as a different skill."""
    return {w.rstrip("s") if len(w) > 3 else w for w in _norm(text).split()}


def _fuzzy_same(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    # A differing token that carries a digit is a VALUE (0402 vs 0603, LM2596
    # vs LM317): that is a contradiction or a different rule, never a
    # restatement -- high word overlap must not collapse the two.
    if any(any(c.isdigit() for c in w) for w in ta ^ tb):
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= FUZZY_OVERLAP


def find_equivalent(doc, text, category):
    """The live skill this text duplicates, or None. Exact normalized-sha
    match wins; else fuzzy token overlap within the same category."""
    sha = _text_sha(text)
    for s in _active(doc):
        if s.get("text_sha256") == sha:
            return s
    for s in _active(doc):
        if s.get("category") == category and _fuzzy_same(s.get("text", ""), text):
            return s
    return None


def _merge_entry(doc, entry):
    """Merge one skill entry into doc by id (tombstone wins, evidence max,
    newest updated_at wins the scalar fields). Used by the outbox drain."""
    for i, s in enumerate(doc["skills"]):
        if s.get("id") == entry.get("id"):
            newer = dict(entry)
            if str(s.get("updated_at", "")) > str(entry.get("updated_at", "")):
                newer = dict(s)
            newer["deleted"] = bool(entry.get("deleted") or s.get("deleted"))
            newer["evidence_count"] = max(int(s.get("evidence_count", 1)),
                                          int(entry.get("evidence_count", 1)))
            doc["skills"][i] = newer
            return
    doc["skills"].append(entry)


def _evict(doc):
    """Keep at most MAX_ACTIVE active skills: archive the weakest first
    (candidates, lowest evidence, oldest)."""
    act = _active(doc)
    if len(act) <= MAX_ACTIVE:
        return
    act.sort(key=lambda s: (s.get("status") == "confirmed",
                            int(s.get("evidence_count", 1)),
                            str(s.get("updated_at", ""))))
    for s in act[:len(act) - MAX_ACTIVE]:
        s["status"] = "archived"
        s["updated_at"] = _now()


def _commit(doc, etag, changed_entries):
    """Push doc (3x conditional-PUT retry re-merging changed_entries on each
    conflict), update the local cache, return 'pushed' | 'queued' | 'disabled'."""
    doc["updated_at"] = _now()
    doc["rev"] = int(doc.get("rev", 0)) + 1
    write_local_cache(doc)
    if not PUSH_ENABLED:
        return "disabled"
    for _ in range(3):
        try:
            _put(doc, etag)
            return "pushed"
        except _Conflict:
            fresh, etag = fetch()
            if fresh is None:
                break
            for e in changed_entries:
                _merge_entry(fresh, e)
            fresh["updated_at"] = _now()
            fresh["rev"] = int(fresh.get("rev", 0)) + 1
            doc = fresh
            write_local_cache(doc)
        except Exception:  # noqa: BLE001 - no creds / offline / anything else
            break
    _queue_outbox(changed_entries)
    return "queued"


def _load_merged():
    """(doc, etag, source): the server doc with the local cache merged in (so
    skills that could not be pushed yet are never lost to a fresh server
    skeleton), or the cache alone when the server is unreachable."""
    doc, etag = fetch()
    cache = load_local_cache()
    if doc is None:
        return cache, None, "local_cache"
    for e in cache.get("skills", []):
        _merge_entry(doc, e)
    # rev is monotonic across BOTH sources: a server skeleton (object not
    # created yet) must not reset the counter the local cache already reached.
    doc["rev"] = max(int(doc.get("rev", 0)), int(cache.get("rev", 0)))
    return doc, etag, "server"


# --------------------------------------------------------------------------
# public operations (the MCP tool calls these; none of them raises)
# --------------------------------------------------------------------------

def save_skill(text, quote, category="other", supersedes="", confirm=False):
    text = str(text).strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    category = _norm(category) or "other"
    if category not in CATEGORIES:
        category = "other"
    ok, why = quote_ok(quote)
    if not ok:
        return {"ok": False, "error": why}

    doc, etag, _ = _load_merged()

    changed = []
    dup = find_equivalent(doc, text, category)
    if dup is not None:
        dup["evidence_count"] = int(dup.get("evidence_count", 1)) + 1
        dup["quote"] = str(quote).strip()
        dup["updated_at"] = _now()
        if dup.get("status") == "candidate" and \
                (confirm or dup["evidence_count"] >= CONFIRM_AT):
            dup["status"] = "confirmed"
        skill, action = dup, "deduped"
    else:
        skill = {
            "id": str(uuid.uuid4()),
            "text": text,
            "category": category,
            "quote": str(quote).strip(),
            "text_sha256": _text_sha(text),
            "author": os.environ.get("ANVIL_USER_EMAIL", ""),
            "created_at": _now(),
            "updated_at": _now(),
            "evidence_count": 1,
            "status": "confirmed" if confirm else "candidate",
            "superseded_by": None,
            "deleted": False,
        }
        doc["skills"].append(skill)
        action = "added"
    changed.append(skill)

    if supersedes:
        for s in doc["skills"]:
            if s.get("id") == supersedes and s is not skill:
                s["status"] = "archived"
                s["superseded_by"] = skill["id"]
                s["updated_at"] = _now()
                changed.append(s)
    _evict(doc)

    # A dedup hit still commits: the evidence bump / promotion must persist,
    # but no NEW entry was created -- that is the "already pushed -> don't
    # push it again" contract.
    push = _commit(doc, etag, changed)
    return {"ok": True, "action": action, "push": push,
            "skill": {k: skill[k] for k in
                      ("id", "text", "category", "status", "evidence_count")}}


def list_skills(include_archived=False):
    doc, _, source = _load_merged()
    if source == "server":
        write_local_cache(doc)
    out = []
    for s in doc.get("skills", []):
        if s.get("deleted"):
            continue
        if s.get("status") == "archived" and not include_archived:
            continue
        out.append({k: s.get(k) for k in
                    ("id", "text", "category", "status", "evidence_count",
                     "author", "updated_at")})
    return {"ok": True, "source": source, "count": len(out), "skills": out}


def update_skill(skill_id, text="", quote="", category=""):
    if text:
        ok, why = quote_ok(quote)
        if not ok:
            return {"ok": False, "error": "changing a skill's text needs a new "
                                          "user quote: " + why}
    doc, etag, _ = _load_merged()
    for s in doc["skills"]:
        if s.get("id") == skill_id and not s.get("deleted"):
            if text:
                s["text"] = str(text).strip()
                s["text_sha256"] = _text_sha(text)
                s["quote"] = str(quote).strip()
            if category and _norm(category) in CATEGORIES:
                s["category"] = _norm(category)
            s["updated_at"] = _now()
            push = _commit(doc, etag, [s])
            return {"ok": True, "push": push, "skill_id": skill_id}
    return {"ok": False, "error": "no such skill id: %s" % skill_id}


def delete_skill(skill_id):
    doc, etag, _ = _load_merged()
    for s in doc["skills"]:
        if s.get("id") == skill_id and not s.get("deleted"):
            s["deleted"] = True                   # tombstone, survives merges
            s["updated_at"] = _now()
            push = _commit(doc, etag, [s])
            return {"ok": True, "push": push, "skill_id": skill_id}
    return {"ok": False, "error": "no such skill id: %s" % skill_id}


def drain_outbox():
    """Retry queued deltas. Called at MCP server start (per chat turn), so a
    machine that was offline catches up automatically. Never raises."""
    try:
        files = sorted(f for f in os.listdir(_outbox()) if f.endswith(".json"))
    except OSError:
        return {"drained": 0}
    if not files:
        return {"drained": 0}
    doc, etag = fetch()
    if doc is None:
        return {"drained": 0, "note": "server unreachable; outbox kept"}
    entries = []
    for name in files:
        try:
            with open(os.path.join(_outbox(), name), "r", encoding="utf-8") as f:
                entries.extend(json.load(f).get("skills", []))
        except Exception:  # noqa: BLE001 - a corrupt outbox file is dropped below
            pass
    for e in entries:
        _merge_entry(doc, e)
    _evict(doc)
    status = _commit(doc, etag, entries)
    if status == "pushed":
        for name in files:
            try:
                os.remove(os.path.join(_outbox(), name))
            except OSError:
                pass
        return {"drained": len(files)}
    return {"drained": 0, "note": "push failed; outbox kept"}
