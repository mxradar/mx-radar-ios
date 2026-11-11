"""Squad identity: replicated TeamComponent IDs, never shared faction IDs."""

def squad_id(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 and value not in (100, 101) else None


def resolve_squad(players, links, actor2slot, self_guid, self_slot, tracker, removed):
    own = players.get(self_guid) or {}
    linked = [a for a, link in links.items() if link.get("ps_guid") == self_guid and self_guid is not None and not removed(a)]
    slotted = [a for a in linked if actor2slot.get(a) == self_slot and self_slot is not None]
    # The local Character need not have a movement slot. Its PlayerState and
    # owning controller are explicit identities, unlike nearby f13 tracks.
    identities = list(dict.fromkeys(slotted + linked + [self_guid, own.get("owner_guid")]))
    own_team = None
    source = "unknown"
    for actor in identities:
        if actor is None:
            continue
        value = squad_id(tracker.current(actor).get("TeamID"))
        if value is not None:
            own_team, source = value, "TeamComponent"
            break
    if own_team is None:
        own_team = squad_id(own.get("team"))
        if own_team is not None:
            source = "PlayerState"
    teams = {}
    mates = {}
    for actor, slot in actor2slot.items():
        link = links.get(actor) or {}
        ps = players.get(link.get("ps_guid")) or {}
        team = squad_id(tracker.current(actor).get("TeamID"))
        if team is None:
            team = squad_id(ps.get("team"))
        teams[slot] = team
        if (own_team is not None and team == own_team and slot != self_slot
                and actor not in linked and not removed(actor)):
            mates[slot] = -2
    return own_team, source, teams, mates
