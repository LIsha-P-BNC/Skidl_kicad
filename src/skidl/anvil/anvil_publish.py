"""Publish Anvil-AI-created library parts to the shared S3 library (and the
local git clones of the library repos, when present).

Flow per part, mirroring the Library Sync plan's dedupe contract:

    1. LOCAL SAVE HAS ALREADY HAPPENED.  Nothing here may ever make a part
       creation fail -- every public function returns a status dict and
       swallows its own errors.
    2. Dedupe BEFORE publish, against S3:
         - fetch the per-type index (anonymous GET, public-read prefix);
         - same name + same sha256     -> "exists"        (skip, by design)
         - same name + different sha   -> "name_conflict" (skip, keep local)
         - absent                      -> publish
    3. Publish = PUT the file + update the index (aws cli, profile/creds of
       the machine), plus a commit into the local git clone of the matching
       library repo when one exists.  If no upload path is available the part
       is queued to an outbox directory and drained on a later attempt.

INTERIM AUTH NOTE: production design routes uploads through the Envil backend
with the login JWT (presigned PUTs, per-user audit).  Until that endpoint
exists, uploads use the machine's AWS CLI credentials; machines without them
accumulate an outbox instead.  The dedupe READ path is anonymous and final.

Environment overrides (all optional):
    ANVIL_LIB_BUCKET       default dms-booma
    ANVIL_LIB_PREFIX       default anvilcad_electronics/anvil_libraries
    ANVIL_LIB_REGION       default ap-south-1
    ANVIL_LIB_AWS_PROFILE  default anvil (ignored if absent)
    ANVIL_LIB_PUBLISH      "0" disables remote publishing entirely
    ANVIL_LIB_GIT_PUSH     "1" additionally pushes the git commit
    ANVIL_SHARED_LIB_DIR   app-visible shared root for local install
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

BUCKET = os.environ.get("ANVIL_LIB_BUCKET", "dms-booma")
PREFIX = os.environ.get("ANVIL_LIB_PREFIX", "anvilcad_electronics/anvil_libraries")
REGION = os.environ.get("ANVIL_LIB_REGION", "ap-south-1")
PROFILE = os.environ.get("ANVIL_LIB_AWS_PROFILE", "anvil")
ENABLED = os.environ.get("ANVIL_LIB_PUBLISH", "1") != "0"
GIT_PUSH = os.environ.get("ANVIL_LIB_GIT_PUSH", "0") == "1"

HTTP_BASE = "https://%s.s3.%s.amazonaws.com" % (BUCKET, REGION)
HTTP_TIMEOUT = 15  # seconds; a slow link must not stall part creation

# kind -> (S3 type folder == repo name, local git clone candidates)
KINDS = {
    "symbol": ("Anvil_Symbols", [r"C:\Envil_project\Anvil_Symbols"]),
    "footprint": ("anvil-footprints", [r"C:\Envil_project\anvil-footprints"]),
    "model3d": ("anvil-packages3D", [r"C:\Envil_project\anvil-packages3D"]),
}


def _shared_root():
    root = os.environ.get("ANVIL_SHARED_LIB_DIR")
    if not root:
        root = os.path.join(os.path.expanduser("~"), "Documents", "kicad",
                            "1.0", "shared-library")
    return root


def _outbox():
    return os.path.join(_shared_root(), ".sync", "outbox")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Anvil-LibPublish/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def _aws(*args):
    """Run the aws cli; returns (rc, output). rc 127 = cli not present."""
    exe = shutil.which("aws")
    if not exe:
        return 127, "aws cli not found"
    cmd = [exe] + list(args)
    if PROFILE:
        cmd += ["--profile", PROFILE]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001 - never raise into part creation
        return 1, str(e)


try:
    from . import anvil_category as _cat
except Exception:  # pragma: no cover - path fallback when skidl pkg deps absent
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "anvil_category", os.path.join(os.path.dirname(__file__), "anvil_category.py"))
    _cat = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_cat)


# --------------------------------------------------------------------------
# symbol monolith merge  (a symbol "category" is ONE .anvil_sym file holding
# many (symbol ...) blocks, so publishing into it is an append, not a new file)
# --------------------------------------------------------------------------

def _top_symbol_spans(text):
    """Yield (start, end, name) for each top-level (symbol "..." ...) block.
    Paren-aware and string-aware; does not use regex for the nesting."""
    i, n = 0, len(text)
    while i < n:
        j = text.find("(symbol", i)
        if j < 0:
            break
        # must be a token boundary after "(symbol"
        k = j + len("(symbol")
        if k < n and text[k] not in " \t\r\n\"":
            i = k
            continue
        # scan the balanced block from the '(' at j
        depth, p, in_str, esc = 0, j, False, False
        name = ""
        got_name = False
        while p < n:
            c = text[p]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                    if not got_name:
                        name = text[name_s:p]
                        got_name = True
                p += 1
                continue
            if c == '"':
                in_str = True
                if not got_name:
                    name_s = p + 1
                p += 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    yield (j, p + 1, name)
                    break
            p += 1
        i = p + 1


def _extract_block(standalone, name):
    """Pull the (symbol "name" ...) block out of a standalone kicad_symbol_lib."""
    for s, e, nm in _top_symbol_spans(standalone):
        if nm == name:
            return standalone[s:e]
    return None


def _merge_into_monolith(base, block, name):
    """Insert `block` into category library text `base`, replacing any existing
    top-level symbol of the same name. Returns (new_text, verdict) with verdict
    'merged' | 'exists' | 'replaced'. Raises on a structurally broken base."""
    spans = list(_top_symbol_spans(base))
    existing = next(((s, e) for s, e, nm in spans if nm == name), None)
    blk = block.strip()

    if existing:
        s, e = existing
        if base[s:e].strip() == blk:
            return base, "exists"
        new = base[:s] + blk + base[e:]
        return new, "replaced"

    # append before the final top-level ')' of the kicad_symbol_lib form
    close = base.rstrip().rfind(")")
    if close < 0:
        raise ValueError("base library has no closing paren")
    head = base[:close].rstrip("\n")
    return head + "\n\t" + blk + "\n)\n", "merged"


def _balanced(text):
    depth, in_str, esc = 0, False, False
    for c in text:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


def _validate_merge(base, merged, name):
    """Hard gate before any upload: the merged category file must be sound."""
    if not _balanced(merged):
        return False, "unbalanced parens after merge"
    base_names = [nm for _, _, nm in _top_symbol_spans(base)]
    new_names = [nm for _, _, nm in _top_symbol_spans(merged)]
    if name not in new_names:
        return False, "new symbol missing after merge"
    # every pre-existing symbol must survive (append case) -- replace case allows
    # the same count; never allow a net loss of other symbols
    lost = set(base_names) - set(new_names)
    if lost:
        return False, "merge dropped existing symbols: %s" % ", ".join(sorted(lost))
    return True, "ok"


def _local_symbol_lib(category):
    """Find the served <category>.anvil_sym / .kicad_sym on this machine, if any."""
    for d in _cat._candidate_dirs("symbol"):
        for ext in (".anvil_sym", ".kicad_sym"):
            p = os.path.join(d, category + ext)
            if os.path.isfile(p):
                return p
    return None


def _safe_component(s):
    """One path component: keep it boring. Rejects traversal outright."""
    s = (s or "").strip()
    if not s or s in (".", "..") or re.search(r'[\\/:*?"<>|\x00-\x1f]', s):
        return ""
    return s


# --------------------------------------------------------------------------
# dedupe index  (S3: <prefix>/index/<type>.json -- public-read like the rest)
# --------------------------------------------------------------------------

def _index_url(type_folder):
    return "%s/%s/index/%s.json" % (HTTP_BASE, PREFIX, type_folder)


def _fetch_index(type_folder):
    try:
        return _http_get_json(_index_url(type_folder))
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return {"schema_version": 1, "parts": {}}   # first ever publish
        raise
    except Exception:
        raise


def _store_index(type_folder, index, workdir):
    path = os.path.join(workdir, "index.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)
    rc, out = _aws("s3", "cp", path,
                   "s3://%s/%s/index/%s.json" % (BUCKET, PREFIX, type_folder),
                   "--content-type", "application/json",
                   "--cache-control", "max-age=30", "--no-progress")
    return rc == 0, out


def lookup(kind, lib, name, content_sha256):
    """The check-before-add. Returns one of:
       {"action":"publish"}                       -- not present remotely
       {"action":"skip","reason":"exists", ...}   -- same part already there
       {"action":"skip","reason":"name_conflict", ...}
       {"action":"publish","reason":"index_unreachable"}  -- fail-open
    """
    type_folder = KINDS[kind][0]
    part_key = "%s/%s" % (lib, name)
    try:
        index = _fetch_index(type_folder)
    except Exception as e:  # index unreachable: publish path will queue/retry
        return {"action": "publish", "reason": "index_unreachable",
                "detail": str(e)[:200]}
    entry = (index.get("parts") or {}).get(part_key)
    if entry is None:
        return {"action": "publish"}
    if entry.get("sha256") == content_sha256:
        return {"action": "skip", "reason": "exists",
                "existing": part_key, "sha256": content_sha256}
    return {"action": "skip", "reason": "name_conflict",
            "existing": part_key, "existing_sha256": entry.get("sha256"),
            "yours_sha256": content_sha256,
            "note": "same name, different content -- shared library keeps the "
                    "original; rename yours to publish it"}


# --------------------------------------------------------------------------
# git side
# --------------------------------------------------------------------------

def _git_commit(kind, rel_path, data, name):
    """Write the file into the local clone of the matching repo and commit.
    Returns a status dict; never raises."""
    repo = next((p for p in KINDS[kind][1] if os.path.isdir(os.path.join(p, ".git"))), None)
    if repo is None:
        return {"git": "no_local_clone"}
    try:
        dest = os.path.join(repo, rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        def g(*a):
            return subprocess.run(["git", "-C", repo] + list(a),
                                  capture_output=True, text=True, timeout=60)
        g("add", "--", rel_path)
        c = g("commit", "-m", "Add AI-generated part %s" % name,
              "--author", "Anvil AI <noreply@anvilcad.com>")
        if c.returncode != 0:
            if "nothing to commit" in (c.stdout + c.stderr):
                return {"git": "unchanged", "repo": repo}
            return {"git": "commit_failed", "detail": (c.stdout + c.stderr)[-200:]}
        out = {"git": "committed", "repo": repo,
               "commit": g("rev-parse", "--short", "HEAD").stdout.strip()}
        if GIT_PUSH:
            p = g("push", "origin", "HEAD")
            out["push"] = "ok" if p.returncode == 0 else "failed: " + (p.stderr or "")[-150:]
        else:
            out["push"] = "skipped (set ANVIL_LIB_GIT_PUSH=1 to push)"
        return out
    except Exception as e:  # noqa: BLE001
        return {"git": "error", "detail": str(e)[:200]}


# --------------------------------------------------------------------------
# outbox (no-credential machines queue instead of publishing)
# --------------------------------------------------------------------------

def _queue_outbox(kind, category, name, filename, data, sha):
    box = _outbox()
    os.makedirs(box, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s-%s-%s" % (stamp, kind, sha[:12])
    with open(os.path.join(box, base + ".bin"), "wb") as fh:
        fh.write(data)
    with open(os.path.join(box, base + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"kind": kind, "category": category, "name": name,
                   "filename": filename, "sha256": sha,
                   "queued_at": stamp}, fh, indent=1)
    return {"s3": "queued", "outbox": box}


def drain_outbox():
    """Retry queued publishes. Safe to call any time; returns a summary."""
    box = _outbox()
    if not os.path.isdir(box):
        return {"drained": 0, "left": 0}
    done = left = 0
    for meta_path in sorted(f for f in os.listdir(box) if f.endswith(".json")):
        full = os.path.join(box, meta_path)
        try:
            with open(full, encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(full[:-5] + ".bin", "rb") as fh:
                data = fh.read()
            res = publish(meta["kind"], meta["name"], data,
                          category=meta.get("category") or meta.get("lib"))
            if res.get("s3") in ("uploaded", None) or res.get("action") == "skip":
                os.remove(full)
                os.remove(full[:-5] + ".bin")
                done += 1
            else:
                left += 1
        except Exception:  # noqa: BLE001
            left += 1
    return {"drained": done, "left": left}


# --------------------------------------------------------------------------
# the publish entry point
# --------------------------------------------------------------------------

def _upload(key, data, content_type, work):
    blob = os.path.join(work, os.path.basename(key))
    with open(blob, "wb") as fh:
        fh.write(data)
    return _aws("s3", "cp", blob, "s3://%s/%s" % (BUCKET, key),
                "--content-type", content_type, "--no-progress")


def _update_index(type_folder, part_key, entry, work):
    try:
        index = _fetch_index(type_folder)
    except Exception:
        index = {"schema_version": 1, "parts": {}}
    index.setdefault("parts", {})[part_key] = entry
    ok, out = _store_index(type_folder, index, work)
    return "updated" if ok else "update_failed: " + out[-150:]


def publish(kind, name, data, meta=None, category=None, lib=None):
    """Dedupe-check then publish one AI-created part into its PROPER CATEGORY in
    S3 + the local git clone.

    kind      'symbol' | 'footprint' | 'model3d'
    name      part name, e.g. 'BQ76942'                 (one path component)
    data      file content (bytes or str)
    meta      optional dict (description/keywords/value/footprint/...) used to
              classify the part into an existing category
    category  optional explicit category hint; used only if it names a real one
    lib       deprecated alias for `category` (back-compat with old callers)

    The category is chosen from the EXISTING library categories only (see
    anvil_category); a symbol is MERGED into <Category>.anvil_sym, a footprint
    dropped into <Category>.pretty/, a 3D model into <Category>.3dshapes/.
    Returns a status dict -- ALWAYS, never raises."""
    try:
        if not ENABLED:
            return {"action": "disabled"}
        if kind not in KINDS:
            return {"action": "error", "detail": "unknown kind %r" % kind}
        name_c = _safe_component(name)
        if not name_c:
            return {"action": "error", "detail": "unsafe name"}
        if isinstance(data, str):
            data = data.encode("utf-8")

        cat, how = _cat.classify(kind, name, meta=meta, hint=category or lib)
        cat_c = _safe_component(cat)
        type_folder = KINDS[kind][0]
        sha = _sha256_bytes(data)
        part_key = "%s/%s" % (cat_c, name_c)

        # dedupe against the per-type index (name keyed by category/part)
        verdict = lookup(kind, cat_c, name_c, sha)
        if verdict["action"] == "skip":
            verdict["category"] = cat_c
            return verdict

        import tempfile
        work = tempfile.mkdtemp(prefix="anvil-pub-")
        try:
            # ---- SYMBOL: merge the block into the category monolith --------
            if kind == "symbol":
                block = _extract_block(data.decode("utf-8", "replace"), name_c)
                if block:
                    base = None
                    base_key = "%s/%s/%s.anvil_sym" % (PREFIX, type_folder, cat_c)
                    rc, _ = _aws("s3", "cp", "s3://%s/%s" % (BUCKET, base_key),
                                 os.path.join(work, "base.anvil_sym"), "--no-progress")
                    if rc == 0:
                        base = open(os.path.join(work, "base.anvil_sym"),
                                    encoding="utf-8", errors="replace").read()
                    elif _local_symbol_lib(cat_c):
                        base = open(_local_symbol_lib(cat_c),
                                    encoding="utf-8", errors="replace").read()
                    if base:
                        try:
                            merged, mv = _merge_into_monolith(base, block, name_c)
                            ok, why = _validate_merge(base, merged, name_c)
                        except Exception as me:  # noqa: BLE001
                            ok, why, mv = False, str(me), "error"
                        if ok:
                            return _finish_symbol_monolith(
                                cat_c, name_c, base_key, merged, sha, mv, work)
                        # fall through to safe per-part file on any doubt
                        safe = {"merge_fallback": why}
                    else:
                        safe = {"merge_fallback": "no base category library found"}
                else:
                    safe = {"merge_fallback": "no (symbol) block in payload"}
                return _publish_discrete(
                    kind, type_folder, cat_c, name_c, data, sha,
                    name_c + ".anvil_sym", "%s.kicad_symdir" % cat_c,
                    "application/octet-stream", work, extra=safe)

            # ---- FOOTPRINT / 3D: drop the file into the category folder ----
            filename, subdir, ctype = {
                "footprint": (name_c + ".anvil_mod", "%s.pretty" % cat_c,
                              "application/octet-stream"),
                "model3d":   (name_c + ".step", "%s.3dshapes" % cat_c,
                              "model/step"),
            }[kind]
            return _publish_discrete(kind, type_folder, cat_c, name_c, data, sha,
                                     filename, subdir, ctype, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception as e:  # noqa: BLE001 - the iron rule: never break a save
        return {"action": "error", "detail": str(e)[:300]}


def _finish_symbol_monolith(cat, name, base_key, merged, sha, mv, work):
    """Upload the merged category library, mirror it into git + the served tree,
    and record the index entry keyed by the part."""
    data = merged.encode("utf-8")
    result = {"action": "published", "kind": "symbol", "part": "%s/%s" % (cat, name),
              "category": cat, "merge": mv, "sha256": sha,
              "s3_key": base_key, "stored": "category_monolith"}
    # git: write the whole category file and commit
    result.update(_git_commit("symbol", "%s.anvil_sym" % cat, data, name))
    # local served tree: keep the running app in sync
    local = _local_symbol_lib(cat)
    if local:
        try:
            tmp = local + ".anvilpub.tmp"
            open(tmp, "w", encoding="utf-8").write(merged)
            os.replace(tmp, local)
            result["local"] = "updated"
        except Exception as e:  # noqa: BLE001
            result["local"] = "skip: " + str(e)[:100]
    rc, out = _upload(base_key, data, "application/octet-stream", work)
    if rc != 0:
        result.update(_queue_outbox("symbol", cat, name, name + ".anvil_sym",
                                    data, sha))
        result["s3_detail"] = out[-200:]
        return result
    result["s3"] = "uploaded"
    result["index"] = _update_index(
        "Anvil_Symbols", "%s/%s" % (cat, name),
        {"sha256": sha, "key": base_key, "kind": "symbol", "category": cat,
         "stored": "category_monolith",
         "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, work)
    return result


def _publish_discrete(kind, type_folder, cat, name, data, sha, filename, subdir,
                      ctype, work, extra=None):
    """Drop one discrete file into <category folder> in S3 + git (footprints, 3D,
    and the symbol fallback path)."""
    rel = "%s/%s" % (subdir, filename)
    key = "%s/%s/%s" % (PREFIX, type_folder, rel)
    result = {"action": "published", "kind": kind, "part": "%s/%s" % (cat, name),
              "category": cat, "sha256": sha, "s3_key": key}
    if extra:
        result.update(extra)
    result.update(_git_commit(kind, rel, data, name))
    rc, out = _upload(key, data, ctype, work)
    if rc != 0:
        result.update(_queue_outbox(kind, cat, name, filename, data, sha))
        result["s3_detail"] = out[-200:]
        return result
    result["s3"] = "uploaded"
    result["index"] = _update_index(
        type_folder, "%s/%s" % (cat, name),
        {"sha256": sha, "key": key, "kind": kind, "category": cat,
         "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, work)
    return result
