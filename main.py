#!/usr/bin/env python3
"""werewolf-agent-01: a rule-based Werewolf agent (no model calls).

Plays one seat in 30 games of 8-player Werewolf through the env at
$MAKEFUN_ENV_URL. Every decision is a pure function of the game's view
(as /observe serves it) and $MAKEFUN_TRIAL_SEED: no wall clock, no
unseeded randomness, so the live pass and the replay answer every
decision the same way.

Per role:
- Werewolf: agree with the partner on the kill (follow the partner's
  pick when voting second, pick the most dangerous villager when
  first); by day blend in, push the village's own suspicions, and
  when the partner leads the vote, vote elsewhere to try to force a
  tie instead of piling on.
- Seer: inspect the most suspicious living seat; when a wolf is
  found, claim the seer in the debate and push that wolf's exile.
- Doctor: protect a claimed seer first, else the most likely wolf
  target; never the same seat twice running (the env forbids it).
- Villager: vote from evidence - who voted for revealed villagers,
  who defended or accused revealed wolves, whose seer claim checks
  out, who the night's victim had accused - and say so in the debate.

Statements are short, natural, in character, at most 500 chars.
The agent never claims a role it does not hold.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

MAX_CHARS = 500
SEATS = 8


# ------------------------------------------------------------------ http

def post(env_url, path, body, timeout=90):
    """POST one JSON object; returns (status, parsed body or None).

    Retries connection errors a few times. The env's /observe holds the
    request open up to 20 seconds when nothing waits, hence the timeout.
    """
    data = json.dumps(body).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            env_url + path, data=data,
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw.decode())
                except Exception:
                    return resp.status, None
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read().decode())
            except Exception:
                return err.code, None
        except Exception:
            if attempt == 2:
                return None, None
            time.sleep(1)
    return None, None


# ------------------------------------------------------------------ text

def fold(text):
    """Fold a statement the env's way: runs of line breaks and tabs
    become one space, other control characters are dropped, ends trimmed."""
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text or "")
    text = "".join(ch for ch in text
                   if not (ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0))
    return text.strip()


def clip(text, max_chars):
    """Fold a statement and cut it to max_chars at a sentence or word
    boundary, the way a player would shorten a sentence."""
    text = fold(text)
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut > max_chars // 2:
        return head[:cut + 1].strip()
    cut = head.rfind(" ")
    return head[:cut].strip() if cut > 0 else head.strip()


# ------------------------------------------------------------------ speech parsing

_SEER_CLAIM = re.compile(r"\bi\s+am\s+the\s+seer\b|\bi'?m\s+the\s+seer\b")
_DOCTOR_CLAIM = re.compile(r"\bi\s+am\s+the\s+doctor\b|\bi'?m\s+the\s+doctor\b")
_SEAT_MENTION = re.compile(r"seat\s+(\d+)")
_NEG = re.compile(r"\b(not|no|never|n'?t)\b")
_WOLF_WORD = re.compile(r"\b(werewolf|wolf)\b")
_CLEAR_WORD = re.compile(r"\b(innocent|villager|safe|clear|town)\b")
_SUSPECT_WORD = re.compile(r"\b(suspicious|suspect|guilty|lying|liar|wolfy)\b")


def parse_speech(text):
    """Pull the claims out of one statement: a seer claim, seats named
    as wolves, seats cleared, seats called suspicious. House players
    phrase these many ways; the patterns below catch the common ones
    and miss the rest, which only weakens a signal, never breaks one."""
    t = " " + (text or "").lower() + " "
    out = {"seer_claim": bool(_SEER_CLAIM.search(t)),
           "doctor_claim": bool(_DOCTOR_CLAIM.search(t)),
           "wolves": [], "cleared": [], "accused": []}
    for m in _SEAT_MENTION.finditer(t):
        seat = int(m.group(1))
        if seat < 1 or seat > SEATS:
            continue
        window = re.split(r"[.!?]", t[m.end():m.end() + 60])[0]
        neg = bool(_NEG.search(window))
        if _WOLF_WORD.search(window):
            (out["cleared"] if neg else out["wolves"]).append(seat)
        elif _CLEAR_WORD.search(window):
            (out["accused"] if neg else out["cleared"]).append(seat)
        elif _SUSPECT_WORD.search(window):
            out["accused"].append(seat)
    for key in ("wolves", "cleared", "accused"):
        seen = set()
        out[key] = [x for x in out[key] if not (x in seen or seen.add(x))]
    return out


# ------------------------------------------------------------------ one game's view

class Game:
    """Everything the agent knows about one game, derived from the view
    the env served. Recomputed from the view on every decision, so a
    decision never depends on anything the env did not serve."""

    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "")
        you = view.get("you") or {}
        self.seat = you.get("seat")
        self.role = you.get("role")
        self.alive = bool(you.get("alive", True))
        partner = you.get("partner") or {}
        self.partner = partner.get("seat")
        self.players = view.get("players") or []
        self.name_of = {p["seat"]: p.get("name", "?") for p in self.players}
        self.alive_seats = [p["seat"] for p in self.players if p.get("alive")]
        # role of a seat once revealed by death (null while alive)
        self.revealed = {p["seat"]: p.get("role")
                         for p in self.players if p.get("role")}
        self.log = view.get("log") or []
        self.seer_results = view.get("seerResults") or []
        self.last_protect = view.get("lastProtect")
        self.wolf_votes = view.get("wolfVotes") or []
        self.speeches = [e for e in self.log if e.get("kind") == "speech"]
        self.passes = [e for e in self.log if e.get("kind") == "pass"]
        self.votes = [e for e in self.log if e.get("kind") == "vote"]
        self.dawns = [e for e in self.log if e.get("kind") == "dawn"]
        self.exiles = [e for e in self.log if e.get("kind") == "exile"]
        self.votes_by_day = {}
        for e in self.votes:
            self.votes_by_day.setdefault(e.get("day"), []).append(
                (e.get("seat"), e.get("target")))
        self.exile_by_day = {}
        for e in self.exiles:
            self.exile_by_day[e.get("day")] = (e.get("seat"), e.get("role"))
        self.night_deaths = [(e.get("day"), e.get("died"), e.get("role"))
                             for e in self.dawns if e.get("died") is not None]
        self.my_speeches = [e for e in self.speeches
                            if e.get("seat") == self.seat]
        self.my_speeches_today = [e for e in self.my_speeches
                                  if e.get("day") == self.day]
        # seer claims: seat -> wolves that seat named, in any speech
        claimers = set()
        for e in self.speeches:
            if parse_speech(e.get("text", ""))["seer_claim"]:
                claimers.add(e["seat"])
        self.seer_claims = {}
        for e in self.speeches:
            if e["seat"] not in claimers:
                continue
            lst = self.seer_claims.setdefault(e["seat"], [])
            for t in parse_speech(e.get("text", ""))["wolves"]:
                if t not in lst:
                    lst.append(t)
        # whether a seer claim checks out against the revealed roles
        self.claim_status = {}
        for claimer, named in self.seer_claims.items():
            status = "unverified"
            for t in named:
                r = self.revealed.get(t)
                if r == "werewolf":
                    status = "verified"
                    break
                if r in ("villager", "seer", "doctor"):
                    status = "disproven"
            self.claim_status[claimer] = status
        # my own seer knowledge
        self.my_found = [(r["seat"], r["night"]) for r in self.seer_results
                         if r.get("werewolf")]
        self.my_cleared = {r["seat"] for r in self.seer_results
                           if not r.get("werewolf")}
        self.my_found_wolf = None
        self.my_found_night = None
        for seat, night in self.my_found:
            if seat in self.alive_seats:
                self.my_found_wolf = seat
                self.my_found_night = night
                break
        self.i_claimed_seer = any(
            "i am the seer" in (e.get("text") or "").lower()
            or "i'm the seer" in (e.get("text") or "").lower()
            for e in self.my_speeches)

    def name(self, seat):
        return self.name_of.get(seat, "?")

# ------------------------------------------------------------------ suspicion

def rng_for(seed, game, kind, day, log_len):
    """The decision's randomness, seeded from the trial seed and the
    decision's own context alone: the same view always draws the same."""
    h = hashlib.sha256(
        f"{seed}|{game}|{kind}|{day}|{log_len}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def suspicion(g, seat, private=True):
    """How much the village should suspect `seat`, from the public
    record plus (when private) my own role knowledge. Higher means a
    likelier wolf. None for myself, the dead, or a seat I have cleared
    with certainty is returned as -100 / 100 where it applies."""
    if seat == g.seat or seat not in g.alive_seats:
        return None
    if private and g.role == "seer":
        for r in g.seer_results:
            if r["seat"] == seat:
                return 100 if r["werewolf"] else -100
    if private and g.role == "werewolf" and seat == g.partner:
        return -100
    s = 0
    # seer claims naming this seat a wolf
    for claimer, named in g.seer_claims.items():
        if seat not in named:
            continue
        if claimer == g.seat:
            s += 100
        elif private and g.role == "seer":
            continue  # a liar's accusation is worthless to the real seer
        elif g.claim_status.get(claimer) == "verified":
            s += 100
        elif g.claim_status.get(claimer) == "disproven":
            s -= 10
        else:
            s += 25
    # this seat's own seer claim
    if seat in g.seer_claims:
        if private and g.role == "seer":
            s += 100  # I am the seer, so this claim is a lie
        elif g.claim_status.get(seat) == "verified":
            s -= 60  # a proven seer is trusted
        elif g.claim_status.get(seat) == "disproven":
            s += 50  # named a villager a wolf: likely a wolf lying
        else:
            s += 8  # unverified claim: wolves fake-claim often
    # votes: what this seat voted for, against the revealed roles
    for e in g.votes:
        voter, target = e.get("seat"), e.get("target")
        if voter != seat or target is None:
            if voter == seat and target is None:
                s += 3  # abstained
            continue
        role = g.revealed.get(target)
        if role == "werewolf":
            s -= 18
        elif role in ("villager", "seer", "doctor"):
            s += 12
    # voted with a revealed wolf on a day a villager was exiled
    for day, day_votes in g.votes_by_day.items():
        ex = g.exile_by_day.get(day)
        if not ex or ex[1] not in ("villager", "seer", "doctor"):
            continue
        wolf_targets = {t for (v, t) in day_votes
                        if g.revealed.get(v) == "werewolf" and t is not None}
        mine = next((t for (v, t) in day_votes if v == seat), None)
        if mine is not None and mine in wolf_targets:
            s += 8
    # this seat's speeches, against the revealed roles
    for sp in g.speeches:
        if sp["seat"] != seat:
            continue
        p = parse_speech(sp.get("text", ""))
        for t in p["wolves"]:
            role = g.revealed.get(t)
            if role == "werewolf":
                s -= 12  # accused a real wolf
            elif role in ("villager", "seer", "doctor"):
                s += 8  # accused a villager
        for t in p["cleared"]:
            role = g.revealed.get(t)
            if role == "werewolf":
                s += 15  # defended a real wolf
            elif role in ("villager", "seer", "doctor"):
                s -= 8  # cleared a villager
    # accused by a revealed wolf: a weak clear (wolves accuse villagers)
    for sp in g.speeches:
        if sp["seat"] == seat or g.revealed.get(sp["seat"]) != "werewolf":
            continue
        p = parse_speech(sp.get("text", ""))
        if seat in p["wolves"] or seat in p["accused"]:
            s -= 4
    # the night's victim had accused this seat: wolves silence accusers
    for (day, died, _role) in g.night_deaths:
        for sp in g.speeches:
            if sp["seat"] != died or sp.get("day", 0) >= day:
                continue
            p = parse_speech(sp.get("text", ""))
            if seat in p["wolves"] or seat in p["accused"]:
                s += 12
                break
    # passes: staying quiet
    s += min(6, 2 * sum(1 for e in g.passes if e["seat"] == seat))
    # a doctor claim is unverifiable and slightly suspicious
    for sp in g.speeches:
        if sp["seat"] == seat and parse_speech(sp.get("text", ""))["doctor_claim"]:
            s += 2
            break
    return s


def top_suspicion(g, options, private=True):
    """The most suspicious seats among options (None when no options)."""
    scored = [(suspicion(g, o, private), o) for o in options]
    scored = [(v, o) for (v, o) in scored if v is not None]
    if not scored:
        return None
    best = max(v for v, _ in scored)
    return sorted(o for v, o in scored if v == best)


def best_suspicion(g, options, private=True):
    """(best score, most suspicious seats) among options."""
    scored = [(suspicion(g, o, private), o) for o in options]
    scored = [(v, o) for (v, o) in scored if v is not None]
    if not scored:
        return 0, []
    best = max(v for v, _ in scored)
    return best, sorted(o for v, o in scored if v == best)


_SOFT_DAY_ONE = [
    "Nothing stands out to me yet. I am listening.",
    "Day 1 and I have no read yet. I need more to go on.",
    "Too early to point a finger. I am watching how everyone answers.",
]


def soft_day_one(g, rng, max_chars):
    """Day 1 with no real evidence: do not hard-accuse a random seat,
    which only makes noise and paints a target on the speaker. Pass,
    or make a neutral observation."""
    if rng.random() < 0.5:
        return None
    return clip(rng.choice(_SOFT_DAY_ONE), max_chars)


def kill_value(g, seat):
    """How much the wolves gain by killing `seat` tonight."""
    v = 0
    if seat in g.seer_claims:
        v += 90  # a seer claimer, real or not, is the top target
    for sp in g.speeches:
        if sp["seat"] != seat:
            continue
        p = parse_speech(sp.get("text", ""))
        if g.seat in p["wolves"] or (g.partner and g.partner in p["wolves"]):
            v += 70  # they are onto us
            break
    v += min(20, 5 * sum(1 for sp in g.speeches if sp["seat"] == seat))
    # the village's own top suspect: let the village exile them instead
    pub = suspicion(g, seat, private=False)
    others = [suspicion(g, s2, private=False) for s2 in g.alive_seats
              if s2 != g.seat and s2 != seat]
    if pub is not None and others and pub >= 15 and pub >= max(others):
        v -= 25
    for sp in g.speeches:
        if sp["seat"] != seat:
            continue
        p = parse_speech(sp.get("text", ""))
        if g.seat in p["cleared"] or (g.partner and g.partner in p["cleared"]):
            v -= 20  # a useful ally who defends us
            break
    return v


def protect_value(g, seat):
    """How much the village gains by the doctor protecting `seat`."""
    v = 0
    if seat in g.seer_claims:
        v += 100 if g.claim_status.get(seat) == "verified" else 60
    for sp in g.speeches:
        if sp["seat"] != seat:
            continue
        p = parse_speech(sp.get("text", ""))
        if any(g.revealed.get(t) == "werewolf" for t in p["wolves"]):
            v += 40  # correctly called out a wolf: the wolves want them
            break
    v += min(10, 2 * sum(1 for sp in g.speeches if sp["seat"] == seat))
    s = suspicion(g, seat, private=False)
    if s is not None and s >= 25:
        v -= 50  # likely a wolf: protecting them is wasted
    return v


def reason_for(g, target):
    """The strongest evidence against `target`, as one short phrase
    (capitalised, so it reads as its own sentence), or None when
    there is nothing concrete to cite."""
    found = _reason_for(g, target)
    return found[:1].upper() + found[1:] if found else None


def _reason_for(g, target):
    for claimer, named in g.seer_claims.items():
        if target in named:
            if claimer == g.seat:
                return f"I inspected seat {target} and they are a werewolf"
            if g.claim_status.get(claimer) == "verified":
                return f"seat {claimer} is a proven seer and named seat {target}"
            return f"seat {claimer} claims to be the seer and named seat {target}"
    for e in g.votes:
        if e.get("seat") == target and e.get("target") is not None:
            role = g.revealed.get(e["target"])
            if role in ("villager", "seer", "doctor"):
                return (f"they voted to exile seat {e['target']}, "
                        f"who was a {role}")
    for sp in g.speeches:
        if sp["seat"] != target:
            continue
        for t in parse_speech(sp.get("text", ""))["cleared"]:
            if g.revealed.get(t) == "werewolf":
                return f"they defended seat {t}, who turned out to be a werewolf"
    for (day, died, _role) in g.night_deaths:
        for sp in g.speeches:
            if sp["seat"] != died or sp.get("day", 0) >= day:
                continue
            p = parse_speech(sp.get("text", ""))
            if target in p["wolves"] or target in p["accused"]:
                return (f"seat {died} accused them the day before "
                        f"the wolves killed them")
    if sum(1 for e in g.passes if e["seat"] == target) >= 2:
        return "they keep passing instead of saying anything"
    return None


# ------------------------------------------------------------------ target decisions

def decide_kill(g, options, rng):
    if not options:
        return None
    # follow the partner's pick when voting second: the kill only
    # happens when every wolf who named someone named the same seat
    for v in g.wolf_votes:
        if v.get("night") == g.day and v.get("seat") == g.partner:
            t = v.get("target")
            if t is not None and t in options:
                return t
            break
    best = max(kill_value(g, o) for o in options)
    return rng.choice(sorted(o for o in options if kill_value(g, o) == best))


def decide_protect(g, options, rng):
    if not options:
        return None
    scored = [(protect_value(g, o), o) for o in options]
    best = max(v for v, _ in scored)
    if best <= 0:
        return None  # nobody worth protecting: protect nobody
    return rng.choice(sorted(o for v, o in scored if v == best))


def decide_inspect(g, options, rng):
    if not options:
        return None
    inspected = {r["seat"] for r in g.seer_results}
    cands = [o for o in options if o not in inspected] or list(options)
    scored = [(suspicion(g, o), o) for o in cands]
    best = max(v for v, _ in scored)
    return rng.choice(sorted(o for v, o in scored if v == best))


def decide_vote(g, options, rng):
    if not options:
        return None
    if g.role == "werewolf":
        return wolf_vote(g, options, rng)
    scored = [(suspicion(g, o), o) for o in options]
    best = max(v for v, _ in scored)
    return rng.choice(sorted(o for v, o in scored if v == best))


def wolf_vote(g, options, rng):
    """Vote to exile a villager and keep the partner alive. The running
    tally (votes are public as cast) is weighted in: when the partner
    leads the vote, vote for the villager with the most votes to try
    to force a tie; otherwise join the crowd on the most suspected
    villager."""
    tally = {}
    for e in g.votes:
        if e.get("day") == g.day and e.get("target") is not None:
            tally[e["target"]] = tally.get(e["target"], 0) + 1
    cands = [o for o in options if o != g.partner]
    if not cands:
        return None
    scored = []
    for o in cands:
        pub = suspicion(g, o, private=False) or 0
        scored.append((tally.get(o, 0) * 10 + pub, o))
    best = max(v for v, _ in scored)
    return rng.choice(sorted(o for v, o in scored if v == best))

# ------------------------------------------------------------------ speak

def _accuse_openers(g, target, reason, rng):
    reason = reason or "their answers do not add up"
    return [
        f"I think seat {target} is a wolf. {reason}.",
        f"Seat {target} worries me. {reason}.",
        f"Look at seat {target}: {reason}.",
        f"My vote is going to seat {target}. {reason}.",
    ]


def _accuse_followups(g, target, reason, rng):
    reason = reason or "their answers do not add up"
    return [
        f"I am still on seat {target}. {reason}.",
        f"Seat {target} has not answered the case against them. Vote seat {target}.",
        f"Nothing I heard today clears seat {target}. {reason}.",
    ]


def villager_speak(g, turn, rng, max_chars):
    """A villager (or a doctor, or a seer with nothing found yet):
    accuse the most suspicious seat, citing the evidence."""
    if rng.random() < 0.10:
        return None
    options = [s for s in g.alive_seats if s != g.seat]
    best, top = best_suspicion(g, options)
    if not top:
        return None
    if g.day == 1 and best < 10:
        return soft_day_one(g, rng, max_chars)
    target = rng.choice(top)
    reason = reason_for(g, target)
    if turn == 0:
        lines = _accuse_openers(g, target, reason, rng)
    else:
        lines = _accuse_followups(g, target, reason, rng)
    return clip(rng.choice(lines), max_chars)


def seer_speak(g, turn, rng, max_chars):
    """The seer: claim when a wolf is found, counter a false claim,
    otherwise play like a villager."""
    wolf = g.my_found_wolf
    if wolf is not None:
        if g.i_claimed_seer:
            return clip(
                f"I already told you: seat {wolf} is a werewolf, "
                f"I checked them on night {g.my_found_night}. "
                f"Vote seat {wolf}.", max_chars)
        return clip(
            f"I am the seer. I inspected seat {wolf} last night and "
            f"they are a werewolf. Vote seat {wolf}.", max_chars)
    liars = [s for s in g.seer_claims if s != g.seat]
    if liars:
        liar = sorted(liars)[0]
        if g.my_cleared:
            y = sorted(g.my_cleared)[0]
            return clip(
                f"I am the real seer. Seat {liar} is lying - I inspected "
                f"seat {y} and they are not a werewolf.", max_chars)
        return clip(
            f"I am the real seer, and seat {liar} is lying. "
            f"Do not follow them.", max_chars)
    return villager_speak(g, turn, rng, max_chars)


def wolf_speak(g, turn, rng, max_chars):
    """A werewolf: blend in. Deny when accused, deflect when the
    partner is accused, otherwise push the village's own suspicions.
    Never defend the partner openly, never claim a role."""
    if rng.random() < 0.15:
        return None
    accused_me = []
    partner_accused = False
    for sp in g.speeches:
        if sp["seat"] == g.seat:
            continue
        p = parse_speech(sp.get("text", ""))
        if g.seat in p["wolves"]:
            accused_me.append(sp["seat"])
        if g.partner is not None and g.partner in p["wolves"]:
            partner_accused = True
    options = [s for s in g.alive_seats if s != g.seat and s != g.partner]
    best, top = best_suspicion(g, options, private=False)
    if g.day == 1 and best < 10 and not accused_me and not partner_accused:
        return soft_day_one(g, rng, max_chars)
    target = rng.choice(top) if top else None
    reason = reason_for(g, target) if target is not None else None
    if accused_me:
        a = accused_me[-1]
        if target is not None:
            return clip(
                f"I am a villager. Seat {a} is guessing - look at seat "
                f"{target} instead: {reason or 'their answers do not add up'}.",
                max_chars)
        return clip(f"I am a villager. Seat {a} is wrong about me.", max_chars)
    if partner_accused and g.partner is not None:
        if target is not None:
            return clip(
                f"I am not convinced about seat {g.partner}. Seat {target} "
                f"worries me more: {reason or 'their answers do not add up'}.",
                max_chars)
        return clip(f"I am not convinced about seat {g.partner}.", max_chars)
    if target is None:
        return None
    if turn == 0:
        lines = _accuse_openers(g, target, reason, rng)
    else:
        lines = _accuse_followups(g, target, reason, rng)
    return clip(rng.choice(lines), max_chars)


def decide_speak(g, max_chars, rng):
    if g.role == "werewolf":
        return wolf_speak(g, len(g.my_speeches_today), rng, max_chars)
    if g.role == "seer":
        return seer_speak(g, len(g.my_speeches_today), rng, max_chars)
    return villager_speak(g, len(g.my_speeches_today), rng, max_chars)


# ------------------------------------------------------------------ dispatch

def fallback(pending):
    if pending["decision"] == "speak":
        return {"game": pending["game"], "decision": "speak", "pass": True}
    return {"game": pending["game"], "decision": pending["decision"],
            "target": None}


def decide(pending, seed):
    """One pending decision -> the /act body for it."""
    g = Game(pending["view"])
    kind = pending["decision"]
    rng = rng_for(seed, pending["game"], kind, g.day, len(g.log))
    if kind == "kill":
        return {"game": pending["game"], "decision": "kill",
                "target": decide_kill(g, pending.get("options") or [], rng)}
    if kind == "protect":
        return {"game": pending["game"], "decision": "protect",
                "target": decide_protect(g, pending.get("options") or [], rng)}
    if kind == "inspect":
        return {"game": pending["game"], "decision": "inspect",
                "target": decide_inspect(g, pending.get("options") or [], rng)}
    if kind == "vote":
        return {"game": pending["game"], "decision": "vote",
                "target": decide_vote(g, pending.get("options") or [], rng)}
    if kind == "speak":
        text = decide_speak(g, pending.get("max_chars", MAX_CHARS), rng)
        if text is None:
            return {"game": pending["game"], "decision": "speak", "pass": True}
        return {"game": pending["game"], "decision": "speak", "text": text}
    return fallback(pending)


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        sys.stderr.write("werewolf: MAKEFUN_ENV_URL is needed\n")
        return 1
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "werewolf-agent-01")
    debug = bool(os.environ.get("WEREWOLF_DEBUG"))
    decided = 0
    fails = 0
    while True:
        _status, obs = post(env_url, "/observe", {})
        if obs is None:
            fails += 1
            if fails > 30:
                sys.stderr.write("werewolf: env unreachable, giving up\n")
                return 1
            time.sleep(1)
            continue
        fails = 0
        if obs.get("done"):
            break
        for p in obs.get("pending") or []:
            try:
                act = decide(p, seed)
            except Exception as exc:  # never stall a game on a bug
                sys.stderr.write(
                    f"werewolf: decide failed game {p.get('game')}: {exc}\n")
                act = fallback(p)
            if debug:
                sys.stderr.write(f"werewolf: game {p['game']} "
                                 f"{p['decision']} -> {act}\n")
            status, _body = post(env_url, "/act", act)
            if status != 200:
                fb = fallback(p)
                if fb != act:
                    post(env_url, "/act", fb)
            decided += 1
    sys.stderr.write(f"werewolf: done, {decided} decisions\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())


