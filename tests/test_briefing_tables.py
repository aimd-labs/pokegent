"""Briefing lists trades the party can do now and tutor moves the party could learn."""

from types import SimpleNamespace

from pokegent.state.briefing import build_briefing

NAMES = {1: "Bulbasaur", 2: "Pikachu", 3: "Mr. Mime", 4: "Abra", 5: "Nidoran"}
DEX = {1: 1, 2: 25, 3: 122, 4: 63, 5: 29}
TUTORS = {0: 100, 1: 200}  # tutor index -> move id
LEARN = {(2, 100), (2, 200), (4, 200)}


class Tables:
    def __init__(self, trades=((3, 4),), tutors=True):
        self.trades = trades
        self.tutors = tutors

    def species_name(self, species):
        return NAMES[species]

    def move_name(self, move):
        return {100: "Mega Punch", 200: "Softboiled"}[move]

    def national_dex(self, species):
        return DEX[species]

    def ingame_trade_table(self):
        return self.trades

    def tutor_move(self, index):
        return TUTORS.get(index) if self.tutors else None

    def can_learn_tutor(self, species, move_id):
        return (species, move_id) in LEARN if self.tutors else None

    def species(self, species_id):
        return SimpleNamespace(types=())

    def move(self, move_id):
        return None

    def type_name(self, type_id):
        return "normal"


def _mon(species, is_egg=False):
    return SimpleNamespace(
        species=species,
        level=5,
        nickname="",
        moves=[],
        hp=20,
        max_hp=20,
        status=0,
        held_item=0,
        is_egg=is_egg,
    )


def _brief(party, tables, owned=(1,)):
    snapshot = SimpleNamespace(
        party=party,
        player=None,
        storage=None,
        bag=(),
        pokedex=SimpleNamespace(owned=set(owned), owned_count=len(owned), seen_count=len(owned)),
    )
    return build_briefing(snapshot, tables=tables, trim=False)


def test_trade_listed_when_party_holds_wanted_and_offer_is_new():
    result = _brief([_mon(1), _mon(4), _mon(4)], Tables())
    assert result["trades_available"] == [
        {"give": "Abra", "get": "Mr. Mime", "party_members": [1, 2]}
    ]


def test_trade_omitted_when_offer_owned_or_wanted_not_in_party():
    assert "trades_available" not in _brief([_mon(4)], Tables(), owned=(122,))
    assert "trades_available" not in _brief([_mon(1)], Tables())


def test_trade_omitted_when_table_unreadable():
    assert "trades_available" not in _brief([_mon(4)], Tables(trades=None))
    assert "trades_available" not in _brief([_mon(4)], None)


def test_tutor_moves_list_party_learners_and_skip_eggs():
    result = _brief([_mon(1), _mon(2), _mon(4), _mon(2, is_egg=True)], Tables())
    assert result["tutor_moves_learnable"] == [
        {"move": "Mega Punch", "party_members": [1]},
        {"move": "Softboiled", "party_members": [1, 2]},
    ]


def test_tutor_key_omitted_when_none_learn_or_table_unreadable():
    assert "tutor_moves_learnable" not in _brief([_mon(1)], Tables())
    assert "tutor_moves_learnable" not in _brief([_mon(2)], Tables(tutors=False))
