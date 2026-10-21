"""Conservative ASR clarification and a small reviewed Lagos area map."""
import difflib
import re
import unicodedata


def norm(value):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()).split())


# These are suggestions requiring confirmation, never automatic eligibility changes.
PLAN_HEARINGS = {"plos": "Plus", "pluss": "Plus", "plusss": "Plus"}
# Observed in the user's ordered pronunciation call, 3b7d4f57f1af.
# Every ASR alias is a proposed place name, requiring caller confirmation.
TOWN_HEARINGS = {
    "eat kedja": "Ikeja", "a kedja": "Ikeja", "kedja": "Ikeja",
    "ike jah": "Ikeja", "ike ja": "Ikeja", "ikejah": "Ikeja", "ikaja": "Ikeja", "eat ketchup": "Ikeja",
    "icoi": "Ikoyi", "e co e": "Ikoyi", "equally": "Ikoyi",
    "i kolodu": "Ikorodu", "ikolodu": "Ikorodu", "i couldu": "Ikorodu",
    "leckie": "Lekki", "licky": "Lekki", "leaky": "Lekki",
    "aca": "Ajah", "aja": "Ajah",
    "oh bah": "Ogba", "obba": "Ogba",
    "bagado": "Gbagada", "bagada": "Gbagada",
    "suruliri": "Surulere", "surinari": "Surulere", "suruniri": "Surulere", "suru leary": "Surulere",
    "lopeduo": "Ilupeju",
    "or on short key": "Oworonshoki",
    "somonu": "Shomolu", "show moduls": "Shomolu",
    "o dzudo bega": "Ojodu Berger",
    "magudu": "Magodo", "magoodoo": "Magodo",
    "i ya ni pa ja": "Iyana Ipaja", "yanai paja": "Iyana Ipaja", "yianni pajang": "Iyana Ipaja",
}
CANONICAL_AREAS = ("Ikeja", "Ikoyi", "Ikorodu", "Lekki", "Ajah", "Ogba", "Gbagada", "Surulere",
                   "Ilupeju", "Oworonshoki", "Shomolu", "Ojodu Berger", "Magodo", "Iyana Ipaja", "Ikotun")
# The ordered exercise suggests intended names, but these ordinary phrases do not
# provide enough speech evidence to select an area in a normal conversation.
UNCLEAR_FRAGMENTS = {"equal to", "in the bedroom", "or shocking", "i got to", "i could", "id rather",
                     "oh wow", "you know pedro", "or a sholky phenomenon", "show my news",
                     "or do the big", "or do the bigger", "i an i project", "i mean i cant tell you"}


def _short_utterance(text):
    heard = norm(text.replace("'", "").replace("’", ""))
    return re.sub(r"^(?:(?:then|and|um|uh|okay|ok|please) )+", "", heard)


def mentioned_locations(text, include_canonical=False):
    heard = norm(text)
    short = _short_utterance(text)
    vocabulary = dict(TOWN_HEARINGS)
    if include_canonical:
        vocabulary.update({norm(x): x for x in CANONICAL_AREAS})
    cues = bool(re.search(r"\b(?:in|around|near|area|location|check for|what about|try|mention|called)\b", heard))
    found = set()
    for alias, target in vocabulary.items():
        if not re.search(r"\b" + re.escape(alias) + r"\b", heard):
            continue
        standalone = bool(re.fullmatch(re.escape(alias) + r"(?: " + re.escape(alias) + r")*", short))
        if cues or standalone:
            found.add(target)
    return found


def new_location(text, pending_filters):
    found = mentioned_locations(text, include_canonical=True)
    return len(found) == 1 and norm(next(iter(found))) != norm(pending_filters.get("town"))

# Area-level alternatives, not distances or travel-time estimates.
# Sources: LASIEC electoral wards, OSM/Mapcarta Ikeja/Oregun and Victoria Island maps.
NEARBY_LAGOS = {
    "ikeja": ["Opebi", "Ogba", "Maryland"],
    "opebi": ["Ikeja", "Maryland", "Ogba"],
    "oregun": ["Ikeja", "Opebi", "Ogba"],
    "alausa": ["Ikeja", "Ogba", "Magodo"],
    "ogba": ["Ikeja", "Agege", "Ojodu"],
    "maryland": ["Ikeja", "Opebi", "Anthony Village"],
    "lagos island": ["Ikoyi", "Victoria Island"],
    "ikoyi": ["Lagos Island", "Victoria Island"],
    "victoria island": ["Ikoyi", "Lagos Island", "Oniru"],
    "oniru": ["Victoria Island", "Lekki"],
}


# Equivalent written town labels found in the supplied sheet. This does not
# normalize caller ASR aliases; those always go through confirmation above.
def town_label(value):
    label = norm(value)
    return {"somolu": "shomolu", "oworoshonki": "oworonshoki"}.get(label, label)


def confirmation(args, changes):
    proposed = dict(args)
    for field, value in changes.items():
        if value is None:
            proposed.pop(field, None)
        else:
            proposed[field] = value
    pieces = []
    if changes.get("town"):
        pieces.append(changes["town"] + (" in Lagos" if norm(proposed.get("state")) == "lagos" else ""))
    if changes.get("plan"):
        pieces.append("the " + changes["plan"] + " plan")
    return {"status": "needs_confirmation", "message": "Just to confirm, did you mean " + " and ".join(pieces) + "?",
            "data": {"suggested_filters": proposed, "corrections": changes, "requires_caller_confirmation": True}}


def clarify(args, transcript=""):
    if args.get("intent") != "lookup":
        return None
    changes = {}
    plan = norm(args.get("plan"))
    if plan.endswith(" plan"):
        plan = plan[:-5]
    heard = norm(transcript)
    for alias, target in PLAN_HEARINGS.items():
        if plan == alias or re.search(r"\b" + re.escape(alias) + r"\s+plan\b", heard):
            changes["plan"] = target
    # Restrict local hypotheses to Lagos. Never rewrite ordinary phrases globally.
    if norm(args.get("state")) in {"", "lagos"}:
        if _short_utterance(transcript) in UNCLEAR_FRAGMENTS:
            return {"status": "needs_input", "message": "I didn't quite catch the area. Could you repeat the name slowly, spell it, or give a nearby landmark?",
                    "data": {"unclear_location": True}}
        targets = mentioned_locations(transcript)
        for field in ("town", "provider"):
            if norm(args.get(field)) in TOWN_HEARINGS:
                targets.add(TOWN_HEARINGS[norm(args[field])])
        if len(targets) > 1:
            return {"status": "needs_input", "message": "I heard more than one possible area. Which area would you like me to check first?",
                    "data": {"town_suggestions": sorted(targets)}}
        if targets:
            changes.update(town=next(iter(targets)), state="Lagos")
            if norm(args.get("provider")) in TOWN_HEARINGS:
                changes["provider"] = None
    return confirmation(args, changes) if changes else None


def fuzzy_choices(value, known):
    normalized = norm(value)
    compact = normalized.replace(" ", "")
    if len(compact) < 4:
        return []
    labels = {norm(x): str(x).strip() for x in known if norm(x)}
    if normalized in labels:
        return []
    scores = sorted(((difflib.SequenceMatcher(None, compact, k.replace(" ", "")).ratio(), v)
                     for k, v in labels.items()), reverse=True)
    return [label for score, label in scores[:2] if score >= .78]


def validate_labels(args, records):
    """Unknown labels are clarified, not treated as evidence of no coverage."""
    if args.get("plan") and args.get("network") in {"PROVIDER", "Gyms"}:
        plans = sorted({re.sub(r"\s+(?:and\s+)?higher$", "", norm(r.get("plan"))) for r in records})
        plans = [p for p in plans if p and p != "all plans"]
        plan = re.sub(r"\s+plan$", "", norm(args["plan"]))
        if plan not in plans:
            choices = fuzzy_choices(plan, plans)
            if len(choices) == 1:
                return confirmation(args, {"plan": choices[0].title()})
            return {"status": "needs_input", "message": "I couldn't recognise that plan name. Please confirm the name on your plan.",
                    "data": {"plan_suggestions": choices, "unknown_plan": args["plan"]}}
    if args.get("town"):
        candidates = [r for r in records if not args.get("state") or norm(r.get("state")) == norm(args["state"])]
        known = {town_label(r.get("town")) for r in candidates}
        town = town_label(args["town"])
        if town not in known and not any(set(town.split()).issubset(k.split()) for k in known) and town not in NEARBY_LAGOS:
            choices = fuzzy_choices(town, [r.get("town", "") for r in candidates])
            if len(choices) == 1:
                return confirmation(args, {"town": choices[0].title()})
            return {"status": "needs_input", "message": "Could you confirm the area name or a nearby landmark? I don't want to search the wrong place.",
                    "data": {"town_suggestions": choices, "unknown_town": args["town"]}}
    return None
