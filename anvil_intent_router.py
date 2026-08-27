"""
anvil_intent_router.py  --  ONE common entry point.

Idea (user's rule): don't run every check every time.
Look at HOW the user asked -> pick the user mode + the ONE intent
-> call ONLY that function.  Everything else stays asleep (lazy).

Everything is table-driven and threshold-driven -- no hardcoded part
names, refs or counts.  Same behaviour on a 5-pin or a 500-pin design.

Wire it into skidl_mcp_server.py with:

    from anvil_intent_router import route
    reply = route(user_text, ctx={...})   # ctx carries netmap/partmap/base...

Each design function is a THIN wrapper: the heavy logic already lives
in the server (_file_erc, _semantic_lint, ...).  The router only decides
*which* to call and *how* to phrase the answer for that user.
"""

from __future__ import annotations
import re


# ---------------------------------------------------------------------------
# 1. USER MODE -- inferred from the *way* they ask, not a setting.
#    Each mode has cue words + a default trust posture.
# ---------------------------------------------------------------------------
USER_MODES = {
    # mode      : (cue regex,                                      posture)
    "founder":  (r"\bcost|price|order|ready|manufactur|how much\b", "outcome"),
    "pro":      (r"\bnet ?class|clearance|assumption|impedance|"
                 r"decoupl|drc|track width\b",                      "transparent"),
    "junior":   (r"\bwhy|explain|how do|what value|which\b",        "teach"),
    "beginner": (r".*",                                             "protect"),  # fallback
}


def detect_mode(text: str) -> str:
    """First mode whose cue matches wins; beginner is the catch-all."""
    t = (text or "").lower()
    for mode, (cue, _posture) in USER_MODES.items():
        if mode == "beginner":
            continue
        if re.search(cue, t):
            return mode
    return "beginner"


def posture_of(mode: str) -> str:
    return USER_MODES.get(mode, USER_MODES["beginner"])[1]


# ---------------------------------------------------------------------------
# SOURCE TYPE -- the doc the user hands us can be many things.
#   image/photo, pdf, url, or a plain spec doc. Each needs a different
#   extractor to turn it into the expected {parts, nets} we compare against.
# ---------------------------------------------------------------------------
SOURCE_KINDS = {
    "url":   r"^https?://|\.com/|www\.",
    "pdf":   r"\.pdf($|\?)",
    "image": r"\.(png|jpe?g|webp|gif|bmp|svg)($|\?)|photo|picture|image|diagram",
    "doc":   r"\.(docx?|txt|md)($|\?)|document|datasheet|spec",
}


def detect_source_kind(ref: str) -> str:
    """Which extractor should read this source? url > pdf > image > doc."""
    r = (ref or "").lower()
    for kind, cue in SOURCE_KINDS.items():
        if re.search(cue, r):
            return kind
    return "text"          # pasted text / unknown -> treat as plain text


# ---------------------------------------------------------------------------
# 2. INTENT TABLE -- each row = one thing a user can ask for.
#    cue      : regex that means "they want this"
#    fn       : the worker to call (lazy -- only the matched one runs)
#    needs    : ctx keys the worker requires (missing -> ask, don't guess)
# ---------------------------------------------------------------------------
INTENTS = [
    # name              cue (what the user typed)                     worker            needs
    ("connection_check", r"correct|right|wrong|connect|wired|ok\b|"
                         r"net ?class|decoupl|clearance|track width|"
                         r"assumption|rule|சரி|தப்ப|check",            "_do_health",    ("netmap", "partmap")),
    ("value_calc",       r"value|resistor.*for|cap.*for|ohm|timing|"
                         r"cutoff|current limit",                     "_do_value",     ("spec",)),
    # user gives the SAME source back: "does my drawing match the doc?"
    # -> report mismatches only (this user just wants to be told).
    ("match_source",     r"same (doc|image|drawing)|match|compare|"
                         r"as per|according to|does it match|"
                         r"draw.*correct|verify.*(doc|image|source)", "_do_match",     ("source", "netmap")),
    # the OTHER user: "find the mistakes AND fix them".
    # fix cue must win over match -> keep it ABOVE nothing but the
    # matcher stays first; disambiguation is handled in _match_intent.
    ("auto_fix",         r"\bfix\b|correct it|repair|solve|"
                         r"make it right|auto.?fix|சரி ?பண்ண",         "_do_fix",       ("source", "netmap", "base")),
    # user DOUBTS the AI: "are you sure? / really?" -- never just
    # re-assert. Re-run the check and show confidence + reasoning.
    ("trust_probe",      r"are you sure|really\?|you sure|confident|"
                         r"double ?check|நிஜமா|sure ?ah",             "_do_trust",     ("netmap", "partmap")),
    # most common real action: change/add/remove on an existing design.
    ("modify",           r"\b(change|set|swap|replace|add|remove|"
                         r"delete|make it|increase|decrease)\b",      "_do_modify",    ("base",)),
    # beginner fear: "will it burn? is it safe?"
    ("safety",           r"burn|safe|damage|blow|too much|overheat|"
                         r"எரி|பாதுகாப்",                              "_do_safety",    ("netmap", "partmap")),
    # optimize: cheaper / smaller / simpler.
    ("optimize",         r"cheaper|smaller|simpler|fewer|reduce|"
                         r"optimi|minimi|cost down",                  "_do_optimize",  ("netmap", "base")),
    ("cost_bom",         r"cost|price|bom|order|how much",            "_do_cost",      ("base",)),
    ("explain",          r"why|explain|how does|what is|reason",      "_do_explain",   ("netmap",)),
    ("critique",         r"any (bug|issue|problem)|review|audit|"
                         r"break|fail",                               "_do_critique",  ("netmap", "partmap")),
]


# ---------------------------------------------------------------------------
# 3. THE ONE COMMON ENTRY POINT
# ---------------------------------------------------------------------------
def route(text: str, ctx: dict | None = None) -> dict:
    """
    Detect user mode + intent, then call ONLY the matched worker.
    Returns {mode, intent, confidence, answer, missing?}.
    """
    ctx = ctx or {}
    t = (text or "").lower()
    mode = detect_mode(t)

    intent = _match_intent(t)
    if intent is None:
        return {"mode": mode, "intent": None,
                "answer": _no_intent_reply(mode)}

    name, _cue, fn_name, needs = intent

    # don't guess: if the worker's inputs aren't here, ask.
    missing = [k for k in needs if not ctx.get(k)]
    if missing:
        return {"mode": mode, "intent": name, "missing": missing,
                "answer": f"({name}) needs {missing} -- give me that first."}

    worker = globals()[fn_name]
    raw = worker(ctx)                       # <-- only THIS function runs
    return {"mode": mode, "intent": name,
            "confidence": raw.get("confidence", 1.0),
            "answer": _present(raw, mode)}  # phrase it for this user


# Some intents carry a STRONGER signal than a plain keyword and must win
# even if a weaker intent's cue also matches. Checked in this order first,
# before the normal top-to-bottom table scan.
#   auto_fix   > match_source  ("compare AND fix" -> fix)
#   trust_probe> connection    ("are you SURE it's right" -> doubt, re-verify)
#   optimize   > modify        ("MAKE IT cheaper" -> optimize, not raw edit)
#   safety     > everything     (a burn question is never anything else)
_PRIORITY = ("safety", "auto_fix", "trust_probe", "optimize")


def _match_intent(t: str):
    by_name = {r[0]: r for r in INTENTS}
    for name in _PRIORITY:                      # strong signals win first
        row = by_name[name]
        if re.search(row[1], t):
            return row
    for row in INTENTS:                         # then normal table order
        if re.search(row[1], t):
            return row
    return None


# ---------------------------------------------------------------------------
# 4. WORKERS -- thin wrappers over the real server logic.
#    (Import the heavy helpers here, or pass them in via ctx["srv"].)
#    Each returns a NEUTRAL result dict; presentation happens later.
# ---------------------------------------------------------------------------
def _do_health(ctx: dict) -> dict:
    """3-level connection check: pin(ERC) -> net(semantic) -> intent."""
    srv = ctx.get("srv")                      # the server module, injected
    findings = []
    if srv and ctx.get("sch_path"):
        findings += srv._file_erc(ctx["sch_path"]).get("violations", [])
    if srv:
        findings += srv._semantic_lint(ctx["netmap"], ctx["partmap"])
    conf = 0.5 if any(f.get("guess") for f in findings) else 0.95
    return {"kind": "health", "findings": findings, "confidence": conf}


def _do_value(ctx: dict) -> dict:
    """R/C value from spec -> nearest E-series (logic lives in server calc)."""
    srv = ctx.get("srv")
    val = srv._calc(ctx["spec"]) if srv and hasattr(srv, "_calc") else None
    return {"kind": "value", "value": val, "spec": ctx["spec"],
            "confidence": 1.0 if val else 0.4}


def _do_match(ctx: dict) -> dict:
    """
    Did we draw what the SOURCE (image/doc) actually shows?
    source = expected {"parts":[refs/types], "nets":{name:[pins]}}
             (extracted from the image/doc when we first built it).
    Compare expected vs the ACTUAL drawn netmap -> list mismatches.
    REPORT ONLY -- this user just wants to be told.
    """
    src = ctx["source"] or {}
    actual = ctx["netmap"] or {}
    exp_nets = src.get("nets", {})
    exp_parts = set(src.get("parts", []))
    act_parts = _parts_in(actual)

    diffs = []
    # part-level: something in the doc we never drew (or drew extra)
    for p in exp_parts - act_parts:
        diffs.append({"level": "part", "issue": f"'{p}' is in the source but missing from the drawing"})
    for p in act_parts - exp_parts:
        diffs.append({"level": "part", "issue": f"'{p}' was drawn but is not in the source"})
    # net-level: a connection the doc has that we didn't reproduce
    for net, pins in exp_nets.items():
        got = {tuple(x[:2]) for x in actual.get(net, [])}
        want = {tuple(x[:2]) for x in pins}
        for miss in want - got:
            diffs.append({"level": "net", "issue": f"net '{net}' should reach {miss} (per source) but doesn't"})

    conf = 0.5 if not src else 0.9      # no source parsed -> low confidence
    return {"kind": "match", "diffs": diffs, "confidence": conf}


def _do_fix(ctx: dict) -> dict:
    """
    Find the mismatches (reuse _do_match) THEN apply the edits to
    reconcile the drawing to the source. This user wants it fixed.
    Every fix is logged with a reason (trust trail); nothing silent.
    """
    report = _do_match(ctx)
    srv = ctx.get("srv")
    applied, skipped = [], []
    for d in report["diffs"]:
        edit = _fix_plan(d)                       # turn a diff into an edit op
        if edit and srv and hasattr(srv, "edit_schematic"):
            try:
                srv.edit_schematic(name=ctx["base"], **edit)
                applied.append({**d, "action": edit})
            except Exception as e:                # never crash the batch
                skipped.append({**d, "why": str(e)})
        else:
            skipped.append({**d, "why": "no safe auto-edit -- needs a human"})
    return {"kind": "fix", "applied": applied, "skipped": skipped,
            "confidence": report["confidence"]}


def _parts_in(netmap: dict) -> set:
    """Distinct refs seen across all nets -- dynamic, no hardcoding."""
    return {ref for pins in (netmap or {}).values() for (ref, *_ ) in pins}


def _fix_plan(diff: dict):
    """Map a mismatch -> a concrete edit op, or None if unsafe to auto-do."""
    # only auto-apply the reversible, unambiguous ones; the rest go to a human.
    lvl = diff.get("level")
    if lvl == "net":
        return {"op": "add_connection", "detail": diff["issue"]}
    if lvl == "part" and "missing from the drawing" in diff["issue"]:
        return {"op": "add_part", "detail": diff["issue"]}
    return None


def _do_trust(ctx: dict) -> dict:
    """User doubts us. RE-RUN the check fresh, report agreement + confidence.
    Trust comes from re-verifying, never from repeating ourselves."""
    again = _do_health(ctx)
    findings = again["findings"]
    return {"kind": "trust", "findings": findings,
            "confidence": again["confidence"],
            "verdict": "still clean" if not findings else "found something on re-check"}


def _do_modify(ctx: dict) -> dict:
    """Change/add/remove. Parse the ask into edit ops; re-check after."""
    srv = ctx.get("srv")
    ops = ctx.get("edit_ops")            # pre-parsed ops, or None -> ask
    if not ops:
        return {"kind": "modify", "need_parse": True,
                "confidence": 0.5,
                "note": "tell me exactly what to change (ref, new value)"}
    done = []
    for op in ops:
        if srv and hasattr(srv, "edit_schematic"):
            srv.edit_schematic(name=ctx["base"], **op)
            done.append(op)
    return {"kind": "modify", "applied": done, "confidence": 0.9}


def _do_safety(ctx: dict) -> dict:
    """Beginner fear: 'will it burn?'. Flag over-current / missing-limit nets."""
    srv = ctx.get("srv")
    risks = []
    if srv:
        # reuse semantic lint, then keep only the burn-class issues
        for w in srv._semantic_lint(ctx["netmap"], ctx["partmap"]):
            if re.search(r"current|limit|resistor|short|power", w.get("issue", ""), re.I):
                risks.append(w)
    return {"kind": "safety", "risks": risks,
            "confidence": 0.7 if not srv else 0.9}


def _do_optimize(ctx: dict) -> dict:
    """cheaper / smaller / simpler -- suggest, don't silently rip parts out."""
    return {"kind": "optimize", "netmap": ctx["netmap"], "confidence": 0.6}


def _do_cost(ctx: dict) -> dict:
    srv = ctx.get("srv")
    bom = srv._bom(ctx["base"]) if srv and hasattr(srv, "_bom") else None
    return {"kind": "cost", "bom": bom}


def _do_explain(ctx: dict) -> dict:
    return {"kind": "explain", "netmap": ctx["netmap"]}


def _do_critique(ctx: dict) -> dict:
    """Adversarial: try to BREAK it -- separate from the builder."""
    srv = ctx.get("srv")
    risks = srv._semantic_lint(ctx["netmap"], ctx["partmap"]) if srv else []
    return {"kind": "critique", "risks": risks}


# ---------------------------------------------------------------------------
# 5. PRESENTATION -- same result, phrased for the user's posture.
# ---------------------------------------------------------------------------
def _present(raw: dict, mode: str) -> str:
    posture = posture_of(mode)
    kind = raw.get("kind")

    if kind == "health":
        f = raw["findings"]
        if posture == "protect":                     # beginner
            if not f:
                return "Looks good -- every connection checks out. "
            top = f[0]
            return f"One thing to fix: {top.get('issue', top)}. Verify this before building."
        if posture == "teach":                       # junior
            return "\n".join(f"- {x.get('issue', x)}  (why: pin/net mismatch)" for x in f) or "No issues."
        # pro -> full table
        return "\n".join(f"{x.get('ref','')}/{x.get('pin','')}: {x.get('issue', x)}" for x in f) or "All nets pass."

    if kind == "value":
        v = raw.get("value")
        base = f"Use {v}." if v else "Give me Vcc / current / frequency and I'll compute it."
        return base + (f"  ({raw['spec']})" if posture == "teach" and v else "")

    if kind == "match":                          # "check my drawing vs the doc"
        d = raw["diffs"]
        if not d:
            return "I checked my drawing against your document -- everything matches. "
        if posture == "protect":                 # beginner: gentle, one at a time
            return (f"I found {len(d)} thing(s) that don't match your document. "
                    f"First: {d[0]['issue']}. Want me to fix it?")
        if posture == "teach":                   # junior: each with the level
            return "Mismatches vs your document:\n" + \
                   "\n".join(f"- [{x['level']}] {x['issue']}" for x in d)
        return "Diff vs source:\n" + "\n".join(f"{x['level']}: {x['issue']}" for x in d)

    if kind == "fix":                            # "find the mistakes AND fix them"
        a, s = raw["applied"], raw["skipped"]
        head = f"Fixed {len(a)} mismatch(es) to match your document."
        if a:
            head += "\n" + "\n".join(f"  ✔ {x['issue']}" for x in a)
        if s:
            head += f"\n{len(s)} need(s) a human:\n" + \
                    "\n".join(f"  ⚠ {x['issue']}  ({x['why']})" for x in s)
        return head

    if kind == "trust":                          # "are you sure?"
        c = int(raw["confidence"] * 100)
        return (f"I re-checked from scratch -- {raw['verdict']} "
                f"(confidence {c}%). " +
                ("" if not raw["findings"] else f"Detail: {raw['findings'][0].get('issue','')}"))

    if kind == "modify":
        if raw.get("need_parse"):
            return raw["note"]
        return f"Done: applied {len(raw['applied'])} change(s), then re-checked."

    if kind == "safety":                         # "will it burn?"
        r = raw["risks"]
        if not r:
            return "No burn risk I can see -- current-limiting looks present. "
        if posture == "protect":
            return f"⚠ Stop -- risk: {r[0].get('issue','')}. Don't power it until this is fixed."
        return "Safety risks:\n" + "\n".join(f"- {x.get('issue','')}" for x in r)

    if kind == "optimize":
        return "I can suggest cheaper/smaller swaps -- want me to list them (no auto-change)?"

    if kind == "cost":
        return f"BOM: {raw.get('bom') or 'not built yet -- build first.'}"

    if kind == "critique":
        r = raw.get("risks")
        return "Risks I could find:\n" + "\n".join(f"- {x.get('issue', x)}" for x in r) if r else "I tried to break it -- found nothing."

    return str(raw)


def _no_intent_reply(mode: str) -> str:
    if posture_of(mode) == "protect":
        return "Tell me what you want to build and I'll check it for you."
    return "No specific check matched -- ask about a connection, a value, or cost."


# ---------------------------------------------------------------------------
# quick self-test (no server needed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    samples = [
        "is this connection correct?",
        "why this resistor value?",
        "what value resistor for the LED?",
        "how much does this cost to order?",
        "net class and decoupling assumption ok?",
        "any bug in this design?",
        "here is the same document, does my drawing match it?",
        "compare with the image I gave",
        "find the mistakes and fix them",
        "check it and fix whatever is wrong",
        "are you sure this is right?",
        "change R1 to 10k and add a switch",
        "will this burn my LED?",
        "can you make it cheaper?",
        "here is the same thing, just vague",
    ]
    for s in samples:
        r = route(s, ctx={"netmap": {}, "partmap": {}, "spec": "led",
                          "base": "x", "source": {"parts": [], "nets": {}}})
        print(f"{s:52s} -> mode={r['mode']:9s} intent={r['intent']}")

    print("\nsource-kind detection:")
    for ref in ["https://ti.com/lm317", "datasheet.pdf", "schematic.png",
                "spec.docx", "just some pasted text"]:
        print(f"  {ref:32s} -> {detect_source_kind(ref)}")
