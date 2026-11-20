"""Symbolically interpret FireRed event scripts into guarded traversal effects.

This module is read-only: unresolved native behavior and operands are reported, never guessed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

from pokegent.games.gen3.firered.rom.script_scan import (
    CONDITIONS,
    ROM_BASE,
    Cmd,
    UnknownOpcode,
    decode_command,
)

GuardKind = Literal["flag", "var", "item", "item_space", "badge", "hm", "trainer"]
# Path epoch, writer site, command, selector/source, optional independent destination.
Origin = tuple[int, int, str, int] | tuple[int, int, str, int, int]


@dataclass(frozen=True)
class Guard:
    kind: GuardKind
    subject: int
    operator: str
    value: int | bool
    origin: Origin | None = field(default=None, repr=False)

    @property
    def identity(self):
        if self.kind != "var":
            return self.kind, self.subject
        source = ("entry", self.subject) if self.origin is None else self.origin
        if self.origin is not None and self.origin[2] == "entry":
            source = ("entry", self.origin[3])
        if self.origin is not None and self.origin[2:] == ("special", 391):
            source = ("entry_quest_log",)
        return self.kind, source

    @property
    def sort_key(self):
        return repr((self.identity, self.operator, self.value))


@dataclass(frozen=True)
class Effect:
    kind: str
    source: int
    guards: tuple[Guard, ...]
    values: tuple[tuple[str, Any], ...] = ()
    decoded: bool = True
    reason: str | None = None

    def value(self, name: str, default: Any = None) -> Any:
        return dict(self.values).get(name, default)


@dataclass(frozen=True)
class Interpretation:
    script_ptr: int
    effects: tuple[Effect, ...]
    unknown: tuple[str, ...] = ()
    commands: int = 0

    @property
    def complete(self) -> bool:
        return not self.unknown and all(effect.decoded for effect in self.effects)


@dataclass(frozen=True)
class ScriptContext:
    player_xy: tuple[int, int] | None = None
    metatile_behavior: Callable[[int], int | None] | None = None
    initial_variables: tuple[tuple[int, int], ...] = ()
    # Native ``DoSeagallopFerryScene`` indexes this ROM table.  The graph decoder supplies the
    # exact bytes from the active ROM profile; an absent or malformed table stays unknown.
    seagallop_warps: tuple[tuple[int, tuple[int, int, int, int, int]], ...] = ()
    preserve_seagallop_special_vars: bool = False
    initial_guards: tuple[Guard, ...] = ()


@dataclass(frozen=True)
class _Comparison:
    kind: GuardKind
    subject: int
    value: int | bool
    result_value: int | None = None
    known_value: int | None = None
    origin: Origin | None = None


@dataclass(frozen=True)
class _State:
    pc: int
    stack: tuple[int, ...] = ()
    guards: tuple[Guard, ...] = ()
    variables: tuple[tuple[int, int], ...] = ()
    comparison: _Comparison | None = None
    steps: int = 0
    origins: tuple[tuple[int, Origin], ...] = ()
    quest_log_entry: bool = True


# IDs from pret/pokefirered 037335f data/specials.inc whose native implementation changes
# traversal-relevant state. Runtime-selected targets stay explicit instead of being inferred.
TRAVERSAL_SPECIALS: dict[int, tuple[str, str, str]] = {
    1: ("set_warp_from_current_tile", "SetCableClubWarp", "src/cable_club.c"),
    2: ("warp", "DoCableClubWarp", "src/cable_club.c"),
    150: ("setflag", "SetHiddenItemFlag", "src/field_specials.c"),
    205: ("setflag", "EnterSafariMode", "src/safari_zone.c"),
    214: ("set_metatile_relative_to_player", "AnimatePcTurnOn", "src/field_specials.c"),
    216: ("setvar", "GetElevatorFloor", "src/field_specials.c"),
    275: ("addobject", "SpawnCameraObject", "src/field_specials.c"),
    276: ("removeobject", "RemoveCameraObject", "src/field_specials.c"),
    309: (
        "set_cracked_ice_metatiles",
        "SetIcefallCaveCrackedIceMetatiles",
        "src/field_specials.c",
    ),
    318: ("warp", "DoDiveWarp", "src/field_control_avatar.c"),
    319: ("warp", "DoFallWarp", "src/field_control_avatar.c"),
    347: ("setvar_choices", "SetVermilionTrashCans", "src/field_specials.c"),
    379: ("warp", "DoSeagallopFerryScene", "src/seagallop.c"),
    404: ("trainer_tower_dispatch", "CallTrainerTowerFunc", "src/trainer_tower.c"),
    427: ("set_object_xy_perm", "DoDeoxysTriangleInteraction", "src/deoxys.c"),
    437: (
        "set_metatiles_relative_to_player",
        "AnimateTeleporterHousing",
        "src/special_field_anim.c",
    ),
    439: (
        "set_metatiles_relative_to_player",
        "AnimateTeleporterCable",
        "src/special_field_anim.c",
    ),
}

# Reviewed native calls that have no traversal effect. References name the implementation at the
# pinned pret revision; these are decoded facts, not a fallback for unclassified specials.
NO_TRAVERSAL_SPECIALS: dict[int, tuple[str, str]] = {
    0: ("HealPlayerParty", "src/script_pokemon_util.c"),
    32: ("EnterColosseumPlayerSpot", "src/cable_club.c"),
    33: ("EnterTradeSeat", "src/cable_club.c"),
    41: ("ChooseHalfPartyForBattle", "src/script_pokemon_util.c"),
    119: ("GetHeracrossSizeRecordInfo", "src/pokemon_size_record.c"),
    121: ("GetMagikarpSizeRecordInfo", "src/pokemon_size_record.c"),
    142: ("DrawWholeMapView", "src/field_camera.c"),
    180: ("GetBattleOutcome", "src/field_specials.c"),
    196: ("ShowBattleRecords", "src/battle_records.c"),
    249: ("BedroomPC", "src/player_pc.c"),
    273: ("AnimateElevator", "src/field_specials.c"),
    286: ("GetRandomSlotMachineId", "src/field_specials.c"),
    306: ("DrawElevatorCurrentFloorWindow", "src/field_specials.c"),
    310: ("ShakeScreen", "src/field_specials.c"),
    344: ("ListMenu", "src/field_specials.c"),
    352: ("CloseElevatorCurrentFloorWindow", "src/field_specials.c"),
    # pret/pokefirered src/trainer_fan_club.c#Script_TryGainNewFanFromCounter: fan counter only.
    170: ("Script_TryGainNewFanFromCounter", "src/trainer_fan_club.c"),
    # pret/pokefirered src/field_specials.c#GetStarterSpecies: reads the starter species.
    354: ("GetStarterSpecies", "src/field_specials.c"),
    355: ("SetSeenMon", "src/field_specials.c"),
    359: ("UpdateTrainerCardPhotoIcons", "src/field_specials.c"),
    368: ("SetWalkingIntoSignVars", "src/script.c"),
    369: ("DisableMsgBoxWalkaway", "src/script.c"),
    371: ("SetFlavorTextFlagFromSpecialVars", "src/fame_checker.c"),
    372: ("UpdatePickStateFromSpecialVar8005", "src/fame_checker.c"),
    381: ("Script_SetHelpContext", "src/help_system.c"),
    391: ("GetQuestLogState", "src/quest_log.c"),
    392: ("QuestLog_CutRecording", "src/quest_log.c"),
    395: ("OpenMuseumFossilPic", "src/script_menu.c"),
    396: ("CloseMuseumFossilPic", "src/script_menu.c"),
    # pret/pokefirered src/party_menu.c#ChooseMonForMoveTutor: opens a party-selection UI.
    397: ("ChooseMonForMoveTutor", "src/party_menu.c"),
    400: ("SetHelpContextForMap", "src/help_system.c"),
    # pret/pokefirered src/event_data.c#IsNationalPokedexEnabled: reads flags/vars only.
    403: ("IsNationalPokedexEnabled", "src/event_data.c"),
    405: ("ShowPokemonJumpRecords", "src/pokemon_jump.c"),
    406: ("BufferTMHMMoveName", "src/field_specials.c"),
    418: ("ShowBerryCrushRankings", "src/berry_crush.c"),
    422: ("ShowDodrioBerryPickingRecords", "src/dodrio_berry_picking.c"),
    423: ("DrawSeagallopDestinationMenu", "src/script_menu.c"),
    424: ("GetSelectedSeagallopDestination", "src/script_menu.c"),
    425: ("GetSeagallopNumber", "src/seagallop.c"),
    429: ("IsPlayerLeftOfVermilionSailor", "src/seagallop.c"),
    434: ("BrailleCursorToggle", "src/field_specials.c"),
    440: ("InitElevatorFloorSelectMenuPos", "src/field_specials.c"),
    442: ("LoopWingFlapSound", "src/field_specials.c"),
    443: ("CreateEnemyEventMon", "src/pokemon.c"),
}

BATTLE_SPECIALS: dict[int, tuple[str, str]] = {
    157: ("StartOldManTutorialBattle", "src/battle_setup.c"),
    312: ("StartLegendaryBattle", "src/battle_setup.c"),
    342: ("StartMarowakBattle", "src/battle_setup.c"),
}

_VERMILION_SWITCH_PAIRS = tuple(
    (first, second)
    for first in range(1, 16)
    for second in range(1, 16)
    if abs((first - 1) % 5 - (second - 1) % 5) + abs((first - 1) // 5 - (second - 1) // 5) == 1
)
_TRAINER_TOWER_FUNCTIONS = (
    "InitTrainerTowerFloor",
    "BufferTowerOpponentSpeech",
    "DoTrainerTowerBattle",
    "TrainerTowerGetChallengeType",
    "TrainerTowerAddFloorCleared",
    "GetFloorAlreadyCleared",
    "StartTrainerTowerChallenge",
    "GetOwnerState",
    "GiveChallengePrize",
    "CheckFinalTime",
    "TrainerTowerResumeTimer",
    "TrainerTowerSetPlayerLost",
    "GetTrainerTowerChallengeStatus",
    "GetCurrentTime",
    "ShowResultsBoard",
    "CloseResultsBoard",
    "TrainerTowerGetDoublesEligiblity",
    "TrainerTowerGetNumFloors",
    "ShouldWarpToCounter",
    "PlayTrainerTowerEncounterMusic",
    "HasSpokenToOwner",
)

_EFFECT_COMMANDS = frozenset(
    {
        "setmetatile",
        "setflag",
        "clearflag",
        "setvar",
        "copyvar",
        "setorcopyvar",
        "addvar",
        "subvar",
        "additem",
        "removeitem",
        "setmaplayoutindex",
        "multichoice",
        "multichoicedefault",
        "multichoicegrid",
        "warp",
        "warpsilent",
        "warpdoor",
        "warphole",
        "warpteleport",
        "warpspinenter",
        "setwarp",
        "setdynamicwarp",
        "setescapewarp",
        "applymovement",
        "applymovementat",
        "removeobject",
        "removeobjectat",
        "addobject",
        "addobjectat",
        "showobjectat",
        "hideobjectat",
        "setobjectxy",
        "setobjectxyperm",
        "copyobjectxytoperm",
        "setobjectmovementtype",
        "opendoor",
        "closedoor",
        "setdooropen",
        "setdoorclosed",
        "settrainerflag",
        "cleartrainerflag",
        "trainerbattle",
    }
)
_TERMINALS = frozenset({"end", "endram", "returnram", "gotonative"})
_NEGATE = {"lt": "ge", "eq": "ne", "gt": "le", "le": "gt", "ge": "lt", "ne": "eq"}
_STD_SCRIPT_COUNT = 10  # pret/pokefirered data/event_scripts.s: gStdScripts..gStdScriptsEnd

# Commands that write VAR_RESULT (gSpecialVar_Result) in pret/pokefirered's
# src/scrcmd.c or through the script menu callbacks. A later comparison must
# not be attributed to an earlier item/HM check after one of these writers.
_VAR_RESULT_WRITERS = {
    "random",
    "additem",
    "removeitem",
    "checkitemtype",
    "addpcitem",
    "checkpcitem",
    "getpartysize",
    "yesnobox",
    "multichoice",
    "multichoicedefault",
    "multichoicegrid",
    "givemon",
    "giveegg",
    "checkmoney",
    "checkplayergender",
    "addcoins",
    "removecoins",
    "checkmonmodernfatefulencounter",
}

# Active yielding paths in pinned scrcmd.c; a future UI read is no longer entry-equivalent.
_YIELDS = frozenset({
    "gotonative", "waitstate", "endram", "animateflash", "fadescreen", "fadescreenspeed",
    "delay", "warp", "warpsilent", "warpdoor", "warphole", "warpteleport", "warpspinenter",
    "waitse", "waitfanfare", "fadeoutbgm", "waitmovement", "waitmovementat", "lockall", "lock",
    "waitmessage", "waitbuttonpress", "yesnobox", "multichoice", "multichoicedefault",
    "multichoicegrid", "hidemonpic", "trainerbattle", "dotrainerbattle", "dowildbattle",
    "pokemart", "pokemartdecoration", "pokemartdecoration2", "playslotmachine",
    "choosecontestmon", "waitfieldeffect", "waitmoncry", "waitdooranim",
})

# These reviewed ferry helpers consume special variables but do not clobber all of them.  Keep
# only their decomp-proven writes; every other special retains the conservative invalidation.
_FERRY_SPECIAL_VAR_WRITES: dict[int, tuple[int, ...] | None] = {
    379: (),  # DoSeagallopFerryScene
    391: None,  # specialvar destination only (GetQuestLogState)
    392: (),  # QuestLog_CutRecording
    423: (0x800D,),  # DrawSeagallopDestinationMenu
    424: None,  # specialvar destination only (GetSelectedSeagallopDestination)
    425: None,  # specialvar destination only (GetSeagallopNumber)
    429: None,  # specialvar destination only (IsPlayerLeftOfVermilionSailor)
}

# Movement semantics are from pret's event_object_movement constants and implementation.
# Stationary turns/animations have zero displacement; ordinary and far jumps use their exact span.
_MOVEMENT: dict[int, tuple[int, int]] = dict.fromkeys(range(0xAA), (0, 0))
_DIRECTIONS = ((0, 1), (0, -1), (-1, 0), (1, 0))
for _start in (
    0x08,
    0x0C,
    0x10,
    0x1D,
    0x31,
    0x35,
    0x39,
    0x3D,
    0x41,
    0x46,
    0x4E,
    0x80,
    0x8C,
    0x90,
    0x94,
    0x9B,
    0xA0,
    0xA6,
):
    for _index, _delta in enumerate(_DIRECTIONS):
        _MOVEMENT[_start + _index] = _delta
for _index, _delta in enumerate(_DIRECTIONS):
    _MOVEMENT[0x14 + _index] = (_delta[0] * 2, _delta[1] * 2)
    _MOVEMENT[0x84 + _index] = (_delta[0] * 2, _delta[1] * 2)
_MOVEMENT[0x6E] = _MOVEMENT[0x6F] = (0, 1)


def _written_registers(command: Cmd, tracked: set[int]) -> set[int]:
    """Pinned scrcmd.c/script_menu.c writes; comparison flags are not VAR_RESULT."""
    name = command.name
    if name in {"setvar", "addvar", "subvar", "copyvar", "setorcopyvar", "checkcoins"}:
        return {command.u16()}
    if name == "getplayerxy":
        return {command.u16(), command.u16(2)}
    if name == "gettime":
        return {0x8000, 0x8001, 0x8002}
    if name == "textcolor":
        return {0x8012, 0x8013}
    if name == "getbraillestringwidth":
        return {0x8004}
    if name == "checkpartymove":
        return {0x800D, 0x8004}
    if name == "checkmoney":
        return {0x800D} if command.operands[4] == 0 else set()
    if name in _VAR_RESULT_WRITERS or name in {"checkitem", "checkitemspace"}:
        return {0x800D}
    if name in {"special", "specialvar"}:
        special = command.u16(2 if name == "specialvar" else 0)
        if special == 391:
            writes = {0x800D}
        elif special == 216:
            writes = {0x403A}
        elif special == 344:
            writes = {0x800D}
        elif special in {273, 306, 352, 440}:
            writes = set()
        elif special in _FERRY_SPECIAL_VAR_WRITES:
            writes = set(_FERRY_SPECIAL_VAR_WRITES[special] or ())
        else:
            writes = tracked | {*range(0x8004, 0x8008), 0x800D}
        return writes | ({command.u16()} if name == "specialvar" else set())
    return set()


def interpret(
    rom: bytes,
    script_ptr: int,
    *,
    std_scripts_ptr: int | None = None,
    context: ScriptContext = ScriptContext(),
    max_steps: int = 20_000,
    max_states: int = 50_000,
) -> Interpretation:
    """Return every guarded traversal effect reachable from ``script_ptr``."""
    todo = deque(
        (
            _State(
                _offset(script_ptr),
                guards=context.initial_guards,
                variables=context.initial_variables,
            ),
        )
    )
    seen: set[tuple[Any, ...]] = set()
    effects: list[Effect] = []
    unknown: set[str] = set()
    command_sites: set[int] = set()
    while todo:
        state = todo.popleft()
        if state.steps >= max_steps:
            unknown.add(f"step limit ({max_steps}) from {script_ptr:#x}")
            continue
        signature = (state.pc, state.stack, state.guards, state.variables, state.comparison,
                     state.origins, state.quest_log_entry)
        if signature in seen:
            continue
        if len(seen) >= max_states:
            unknown.add(f"state limit ({max_states}) from {script_ptr:#x}")
            break
        seen.add(signature)
        try:
            command = decode_command(rom, state.pc)
        except UnknownOpcode as exc:
            unknown.add(str(exc))
            continue
        command_sites.add(command.addr)
        next_pc = command.addr + 1 + len(command.operands)
        prior_variables = dict(state.variables)
        variables = dict(prior_variables)
        origins = dict(state.origins)
        writes = _written_registers(command, set(variables) | set(origins))
        special = command.u16(2 if command.name == "specialvar" else 0) if command.name in {
            "special", "specialvar"} else None
        for variable in writes:
            variables.pop(variable, None)
            name = command.name
            if special == 391 and not state.quest_log_entry:
                name = "special_unbound"
            selector = special if special is not None else variable
            if special == 344:
                name, selector = "list_menu", prior_variables.get(0x8004, -1)
            origins[variable] = (state.steps + 1, command.addr, name,
                                 selector)
            # Native outputs are independent; only the proven 391 RESULT stays canonical.
            if special is not None and not (special == 391 and variable == 0x800D):
                origins[variable] += (variable,)
        comparison = _comparison(command, prior_variables, state.comparison, origins)
        if command.name == "setvar":
            variables[command.u16()] = command.u16(2)
        elif command.name in {"addvar", "subvar"}:
            variable, operand = command.u16(), command.u16(2)
            operand = (operand if command.name == "addvar" or operand < 0x4000
                       else prior_variables.get(operand))
            if variable in prior_variables and operand is not None:
                delta = operand if command.name == "addvar" else -operand
                variables[variable] = (prior_variables[variable] + delta) & 0xFFFF
            else:
                variables.pop(variable, None)
        elif command.name in {"copyvar", "setorcopyvar"}:
            dest, source = command.u16(), command.u16(2)
            if command.name == "setorcopyvar" and source < 0x4000:
                variables[dest] = source
            else:
                if source in prior_variables:
                    variables[dest] = prior_variables[source]
                origins[dest] = dict(state.origins).get(source, (0, 0, "entry", source))
        elif command.name == "gettime":
            variables.update({variable: 0 for variable in writes})
        elif command.name == "textcolor":
            variables[0x8012] = command.operands[0]
            if 0x8012 in prior_variables:
                variables[0x8013] = prior_variables[0x8012]
            origins[0x8013] = dict(state.origins).get(0x8012, (0, 0, "entry", 0x8012))
        base = _State(
            next_pc,
            state.stack,
            state.guards,
            tuple(sorted(variables.items())),
            comparison,
            state.steps + 1,
            tuple(sorted(origins.items())),
            state.quest_log_entry and command.name not in _YIELDS
            and (special is None or special in {216, 391, 424, 425, 429}),
        )

        if command.name in {"setptr", "setptrbyte", "copybyte"}:
            unknown.add(f"unresolved memory write at {command.addr:#x}")

        if command.name in _EFFECT_COMMANDS:
            effects.append(_effect(rom, command, state.guards, prior_variables, context))
        elif command.name in {"special", "specialvar"}:
            special_id = command.u16(2 if command.name == "specialvar" else 0)
            special_effects = _special_effect(
                command, state.guards, special_id, prior_variables, context
            )
            effects.extend(
                special_effects if isinstance(special_effects, tuple) else (special_effects,)
            )
        elif command.name in {"callnative", "gotonative"}:
            reason = f"native call {command.u32():#010x} has no event-script semantics"
            effects.append(
                Effect("unknown_native", command.addr, state.guards, decoded=False, reason=reason)
            )

        if command.name in _TERMINALS:
            continue
        if command.name == "return":
            if state.stack:
                todo.append(replace(base, pc=state.stack[-1], stack=state.stack[:-1]))
            continue
        if command.name == "goto":
            _enqueue_target(todo, base, command.target, unknown)
            continue
        if command.name == "call":
            _enqueue_target(
                todo, replace(base, stack=(*state.stack, next_pc)), command.target, unknown
            )
            continue
        if command.name in {"goto_if", "call_if"}:
            _branch(
                todo,
                base,
                command.name == "call_if",
                command.target,
                command.operands[0],
                state.comparison,
                unknown,
            )
            continue
        if command.name in {"gotostd", "callstd"}:
            target = _std_target(rom, std_scripts_ptr, command.operands[0], unknown)
            stack = (*state.stack, next_pc) if command.name == "callstd" else state.stack
            _enqueue_target(todo, replace(base, stack=stack), target, unknown)
            continue
        if command.name in {"gotostd_if", "callstd_if"}:
            target = _std_target(rom, std_scripts_ptr, command.operands[1], unknown)
            _branch(
                todo,
                base,
                command.name == "callstd_if",
                target,
                command.operands[0],
                state.comparison,
                unknown,
            )
            continue
        if command.trainer_script_target is not None:
            trainer = command.u16(1) if len(command.operands) >= 3 else 0
            guarded = _add_guard(state.guards, Guard("trainer", trainer, "eq", True))
            if guarded is not None:
                todo.append(replace(base, pc=command.trainer_script_target, guards=guarded))
        todo.append(base)
    return Interpretation(
        script_ptr, tuple(dict.fromkeys(effects)), tuple(sorted(unknown)), len(command_sites)
    )


def _comparison(
    command: Cmd, variables: dict[int, int], current: _Comparison | None,
    origins: dict[int, Origin],
) -> _Comparison | None:
    if command.name == "checkflag":
        flag = command.u16()
        return _Comparison("badge" if 0x820 <= flag <= 0x827 else "flag", flag, True)
    if command.name == "checktrainerflag":
        return _Comparison("trainer", command.u16(), True)
    if command.name == "checkitem":
        return _Comparison("item", command.u16(), command.u16(2), origin=origins.get(0x800D))
    if command.name == "checkitemspace":
        return _Comparison("item_space", command.u16(), command.u16(2),
                           origin=origins.get(0x800D))
    if command.name == "checkpartymove":
        return _Comparison("hm", command.u16(), True, origin=origins.get(0x800D))
    if command.name in _VAR_RESULT_WRITERS:
        return None
    if command.name in {"setvar", "addvar", "subvar", "copyvar", "setorcopyvar"}:
        if command.u16() == 0x800D:
            return None
    if command.name == "checkcoins" and command.u16() == 0x800D:
        return None
    if command.name == "compare_var_to_value":
        if (
            command.u16() == 0x800D
            and current is not None
            and current.kind
            in {
                "item",
                "item_space",
                "hm",
            }
            and current.origin == origins.get(0x800D)
        ):
            return replace(current, result_value=command.u16(2))
        variable = command.u16()
        return _Comparison("var", variable, command.u16(2), known_value=variables.get(variable),
                           origin=origins.get(variable))
    if command.name == "compare_var_to_var" and command.u16(2) in variables:
        variable = command.u16()
        return _Comparison(
            "var",
            variable,
            variables[command.u16(2)],
            known_value=variables.get(variable),
            origin=origins.get(variable),
        )
    if command.name.startswith("compare_") or command.name in {"special", "specialvar"}:
        return None
    return current


def _branch(
    todo: deque[_State],
    base: _State,
    is_call: bool,
    target: int | None,
    condition: int,
    comparison: _Comparison | None,
    unknown: set[str],
) -> None:
    base = replace(base, comparison=None)
    if comparison is None or condition not in CONDITIONS:
        unknown.add(f"branch at {base.pc:#x} has no symbolic comparison")
        todo.append(base)
        _enqueue_target(
            todo,
            replace(base, stack=(*base.stack, base.pc) if is_call else base.stack),
            target,
            unknown,
        )
        return
    if comparison.kind == "var" and comparison.known_value is not None:
        matches = _condition_matches(comparison.known_value, comparison.value, condition)
        if matches:
            _enqueue_target(
                todo,
                replace(base, stack=(*base.stack, base.pc) if is_call else base.stack),
                target,
                unknown,
            )
        else:
            todo.append(base)
        return
    if comparison.kind in {"flag", "badge", "trainer"}:
        if condition not in {0, 1}:
            unknown.add(
                f"branch at {base.pc:#x} uses non-canonical {CONDITIONS[condition]} "
                f"comparison for {comparison.kind}"
            )
            todo.append(base)
            _enqueue_target(
                todo,
                replace(base, stack=(*base.stack, base.pc) if is_call else base.stack),
                target,
                unknown,
            )
            return
        branch_value = bool(condition)
        yes = Guard(comparison.kind, comparison.subject, "eq", branch_value)
        no = Guard(comparison.kind, comparison.subject, "eq", not branch_value)
    elif comparison.kind in {"item", "item_space", "hm"}:
        guards = _result_guards(comparison, condition)
        if guards is None:
            unknown.add(
                f"branch at {base.pc:#x} cannot map {comparison.kind} result comparison "
                f"{CONDITIONS[condition]} {comparison.result_value!r}"
            )
            todo.append(base)
            _enqueue_target(
                todo,
                replace(base, stack=(*base.stack, base.pc) if is_call else base.stack),
                target,
                unknown,
            )
            return
        yes, no = guards
    else:
        operator = CONDITIONS[condition]
        yes = Guard(comparison.kind, comparison.subject, operator, comparison.value,
                    comparison.origin)
        no = Guard(comparison.kind, comparison.subject, _NEGATE[operator], comparison.value,
                   comparison.origin)
    no_guards = _add_guard(base.guards, no)
    if no_guards is not None:
        todo.append(replace(base, guards=no_guards))
    yes_guards = _add_guard(base.guards, yes)
    if yes_guards is not None:
        branch = replace(
            base,
            guards=yes_guards,
            stack=(*base.stack, base.pc) if is_call else base.stack,
        )
        _enqueue_target(todo, branch, target, unknown)


def _result_guards(comparison: _Comparison, condition: int) -> tuple[Guard, Guard] | None:
    """Map a concrete VAR_RESULT comparison back to the fact that produced it."""
    target = comparison.result_value
    if target is None:
        return None
    if comparison.kind in {"item", "item_space"}:
        present_matches = _condition_matches(1, target, condition)
        absent_matches = _condition_matches(0, target, condition)
        if present_matches == absent_matches:
            return None
        positive = Guard(comparison.kind, comparison.subject, "ge", comparison.value)
        negative = Guard(comparison.kind, comparison.subject, "lt", comparison.value)
        return (positive, negative) if present_matches else (negative, positive)
    if comparison.kind == "hm":
        # CheckPartyHasMove returns the party slot (0..5), or PARTY_SIZE (6) when absent.
        present = {_condition_matches(slot, target, condition) for slot in range(6)}
        if len(present) != 1:
            return None
        present_matches = present.pop()
        absent_matches = _condition_matches(6, target, condition)
        if present_matches == absent_matches:
            return None
        positive = Guard("hm", comparison.subject, "eq", True)
        negative = Guard("hm", comparison.subject, "eq", False)
        return (positive, negative) if present_matches else (negative, positive)
    return None


def _condition_matches(actual: int, expected: int, condition: int) -> bool:
    operator = CONDITIONS[condition]
    return {
        "lt": actual < expected,
        "eq": actual == expected,
        "gt": actual > expected,
        "le": actual <= expected,
        "ge": actual >= expected,
        "ne": actual != expected,
    }[operator]


def _std_target(rom: bytes, table: int | None, index: int, unknown: set[str]) -> int | None:
    if table is None:
        unknown.add(f"standard script {index} cannot be followed: gStdScripts unavailable")
        return None
    if not 0 <= index < _STD_SCRIPT_COUNT:
        unknown.add(f"standard script {index} is outside gStdScripts (count {_STD_SCRIPT_COUNT})")
        return None
    at = _offset(table) + index * 4
    if not 0 <= at <= len(rom) - 4:
        unknown.add(f"standard script {index} table entry is outside ROM")
        return None
    return _offset(int.from_bytes(rom[at : at + 4], "little"))


def _enqueue_target(
    todo: deque[_State], state: _State, target: int | None, unknown: set[str]
) -> None:
    if target is None:
        unknown.add(f"invalid script target from {state.pc:#x}")
    else:
        todo.append(replace(state, pc=target))


def _effect(
    rom: bytes,
    command: Cmd,
    guards: tuple[Guard, ...],
    variables: dict[int, int],
    context: ScriptContext,
) -> Effect:
    name, values, decoded, reason = command.name, {}, True, None
    if name == "setmetatile":
        metatile = _resolve(command.u16(4), variables)
        values = {
            "x": _resolve(command.u16(), variables),
            "y": _resolve(command.u16(2), variables),
            "metatile": metatile,
            "collision": command.u16(6),
        }
        values["behavior"] = (
            context.metatile_behavior(metatile)
            if context.metatile_behavior and isinstance(metatile, int)
            else None
        )
        if values["behavior"] is None:
            reason = "metatile behavior unavailable without the map tileset"
    elif name in {"setflag", "clearflag", "settrainerflag", "cleartrainerflag"}:
        values = {
            "flag": _resolve(command.u16(), variables),
            "set": name in {"setflag", "settrainerflag"},
        }
    elif name == "setvar":
        values = {"var": command.u16(), "value": _resolve(command.u16(2), variables)}
    elif name in {"copyvar", "setorcopyvar"}:
        values = {"var": command.u16(), "value": _resolve(command.u16(2), variables)}
    elif name in {"addvar", "subvar"}:
        variable, operand = command.u16(), command.u16(2)
        current = variables.get(variable)
        term = operand if name == "addvar" or operand < 0x4000 else variables.get(operand)
        delta = term if name == "addvar" else -term if term is not None else None
        values = {
            "var": variable,
            "operand": operand,
            "value": ((current + delta) & 0xFFFF
                      if current is not None and delta is not None else None),
        }
        if current is None:
            reason = f"variable {variable:#06x} has no tracked value before {name}"
    elif name == "setmaplayoutindex":
        values = {"layout": _resolve(command.u16(), variables)}
    elif name in {"multichoice", "multichoicedefault", "multichoicegrid"}:
        values = {"menu_id": command.operands[2]}
    elif name in {"additem", "removeitem"}:
        values = {
            "item": _resolve(command.u16(), variables),
            "count": _resolve(command.u16(2), variables),
        }
    elif name in {
        "warp",
        "warpsilent",
        "warpdoor",
        "warpteleport",
        "setwarp",
        "setdynamicwarp",
        "setescapewarp",
        "warpspinenter",
    }:
        values = {
            "group": command.operands[0],
            "map": command.operands[1],
            "warp": command.operands[2],
            "x": command.u16(3),
            "y": command.u16(5),
        }
    elif name == "warphole":
        values = {"group": command.operands[0], "map": command.operands[1]}
    elif name in {"applymovement", "applymovementat"}:
        local_id, pointer = _resolve(command.u16(), variables), command.u32(2)
        path, reason = _movement_path(rom, pointer)
        delta = (
            (sum(step[0] for step in path), sum(step[1] for step in path))
            if path is not None
            else None
        )
        values = {"local_id": local_id, "movement_ptr": pointer, "path": path, "delta": delta}
        if name == "applymovementat":
            values.update(map=command.operands[6], group=command.operands[7])
        if (
            name == "applymovement"
            and local_id == 0xFF
            and context.player_xy is not None
            and delta is not None
        ):
            values["end"] = (context.player_xy[0] + delta[0], context.player_xy[1] + delta[1])
        elif name == "applymovement" and local_id == 0xFF:
            values["end"] = None
            reason = reason or "player start tile unavailable"
        decoded = path is not None
    elif name in {"removeobject", "addobject", "copyobjectxytoperm"}:
        values = {"local_id": _resolve(command.u16(), variables)}
    elif name in {"removeobjectat", "addobjectat"}:
        values = {
            "local_id": _resolve(command.u16(), variables),
            "map": command.operands[2],
            "group": command.operands[3],
        }
    elif name in {"showobjectat", "hideobjectat"}:
        values = {
            "local_id": _resolve(command.u16(), variables),
            "map": command.operands[2],
            "group": command.operands[3],
            "visible": name == "showobjectat",
        }
    elif name in {"setobjectxy", "setobjectxyperm"}:
        values = {
            "local_id": _resolve(command.u16(), variables),
            "x": _resolve(command.u16(2), variables),
            "y": _resolve(command.u16(4), variables),
        }
    elif name == "setobjectmovementtype":
        values = {
            "local_id": _resolve(command.u16(), variables),
            "movement_type": command.operands[2],
        }
    elif name in {"opendoor", "closedoor", "setdooropen", "setdoorclosed"}:
        values = {
            "x": _resolve(command.u16(), variables),
            "y": _resolve(command.u16(2), variables),
            "open": name in {"opendoor", "setdooropen"},
            "animated": name in {"opendoor", "closedoor"},
        }
    elif name == "trainerbattle":
        values = {"trainer": command.u16(1)}
    return Effect(name, command.addr, guards, tuple(values.items()), decoded, reason)


def _special_effect(
    command: Cmd,
    guards: tuple[Guard, ...],
    special_id: int,
    variables: dict[int, int],
    context: ScriptContext,
) -> Effect | tuple[Effect, ...]:
    battle = BATTLE_SPECIALS.get(special_id)
    if battle is not None:
        name, source_file = battle
        return Effect(
            "battle_start",
            command.addr,
            guards,
            (
                ("special", special_id),
                ("name", name),
                ("reference", f"pret/pokefirered@037335f:{source_file}#{name}"),
                ("whiteout_modelled", False),
            ),
            reason="battle start is modelled; any later whiteout warp is outside this script",
        )
    irrelevant = NO_TRAVERSAL_SPECIALS.get(special_id)
    if irrelevant is not None:
        name, reference = irrelevant
        values: tuple[tuple[str, Any], ...] = (
            ("special", special_id),
            ("name", name),
            ("reference", f"pret/pokefirered@037335f:{reference}#{name}"),
        )
        if special_id == 423:
            values = (
                *values,
                ("menu_id", 0xFF),
                ("origin", _resolve(0x8004, variables)),
                ("page", _resolve(0x8005, variables)),
            )
        return Effect(
            "no_traversal_effect",
            command.addr,
            guards,
            values,
            reason="reviewed native implementation has no traversal-state mutation",
        )
    known = TRAVERSAL_SPECIALS.get(special_id)
    if known is None:
        reason = f"special {special_id} has no reviewed traversal classification"
        return Effect(
            "unknown_special", command.addr, guards, (("special", special_id),), False, reason
        )
    kind, name, source_file = known
    values: dict[str, Any] = {
        "special": special_id,
        "name": name,
        "reference": f"pret/pokefirered@037335f:{source_file}#{name}",
    }
    if special_id == 150:
        values.update(flag=_resolve(0x8004, variables), set=True)
    elif special_id == 205:
        values.update(flag=0x800, set=True)
    elif special_id == 214:
        values.update(
            selector=_resolve(0x8004, variables),
            player_xy=context.player_xy,
            position="facing-dependent PC tile adjacent to player",
            metatile_choices=((0, 0x063), (1, 0x28A), (2, 0x28A)),
            collision=1,
        )
    elif special_id == 216:
        values.update(var=0x403A, value=None, var_name="VAR_ELEVATOR_FLOOR")
    elif special_id in {275, 276}:
        values.update(local_id=127, position="player tile" if special_id == 275 else None)
    elif special_id == 347:
        values.update(vars=(0x8004, 0x8005), choices=_VERMILION_SWITCH_PAIRS)
    elif special_id == 379:
        selector = _resolve(0x8006, variables)
        values["selector"] = selector
        warps = dict(context.seagallop_warps)
        if not warps:
            return Effect(
                kind,
                command.addr,
                guards,
                tuple(values.items()),
                False,
                "Seagallop destination table is unavailable from the ROM profile",
            )
        if isinstance(selector, int) and selector in warps:
            group, map_number, warp, x, y = warps[selector]
            values.update(group=group, map=map_number, warp=warp, x=x, y=y)
        else:
            values["targets"] = tuple(sorted(warps.items()))
    elif special_id == 404:
        selector = _resolve(0x8004, variables)
        values["selector"] = selector
        if isinstance(selector, int) and 0 <= selector < len(_TRAINER_TOWER_FUNCTIONS):
            values["function"] = _TRAINER_TOWER_FUNCTIONS[selector]
        else:
            values["functions"] = _TRAINER_TOWER_FUNCTIONS
        if selector == 0:
            kind = "set_map_layout"
            values["layout"] = "trainer tower floor/challenge layout from saved tower data"
    elif special_id == 437:
        values.update(
            selector=_resolve(0x8004, variables),
            player_xy=context.player_xy,
            positions="(player x + 6 or -1, player y - 5) and y + 2",
            final_metatiles=(0x28A, 0x296),
            collision=1,
        )
    elif special_id == 439:
        values.update(
            player_xy=context.player_xy,
            positions="four cable rows from player x + 4, player y - 5",
            final_metatiles=(0x285, 0x2B4),
            collision=1,
        )
    unresolved = kind in {
        "set_map_layout",
        "set_metatile_relative_to_player",
        "set_metatiles_relative_to_player",
        "set_warp_from_current_tile",
        "set_object_xy_perm",
    }
    if special_id in {318, 319}:
        unresolved = True
    if special_id == 404 and not isinstance(values.get("selector"), int):
        unresolved = True
    reason = (
        "native effect destination depends on unresolved live engine state" if unresolved else None
    )
    return Effect(kind, command.addr, guards, tuple(values.items()), not unresolved, reason)


def _movement_path(
    rom: bytes, pointer: int
) -> tuple[tuple[tuple[int, int], ...] | None, str | None]:
    at, path = _offset(pointer), []
    for _ in range(256):
        if not 0 <= at < len(rom):
            return None, "movement stream is outside ROM"
        action = rom[at]
        if action == 0xFE:
            return tuple(path), None
        if action not in _MOVEMENT:
            return None, f"movement action {action:#04x} has no end-tile displacement"
        path.append(_MOVEMENT[action])
        at += 1
    return None, "movement stream exceeds 256 actions"


def _resolve(value: int, variables: dict[int, int]) -> int | str:
    return value if value < 0x4000 else variables.get(value, f"var:{value:#06x}")


def _add_guard(guards: tuple[Guard, ...], guard: Guard) -> tuple[Guard, ...] | None:
    """Add a guard, rejecting paths proven contradictory by exact comparisons."""
    if guard in guards:
        return guards
    for existing in guards:
        if existing.identity != guard.identity:
            continue
        if guard.operator == "eq" and (
            (existing.operator == "eq" and existing.value != guard.value)
            or (existing.operator == "ne" and existing.value == guard.value)
        ):
            return None
        if guard.operator == "ne" and existing.operator == "eq" and existing.value == guard.value:
            return None
    return (*guards, guard)


def _offset(pointer: int) -> int:
    return pointer - ROM_BASE if pointer >= ROM_BASE else pointer


__all__ = [
    "BATTLE_SPECIALS",
    "Effect",
    "Guard",
    "Interpretation",
    "NO_TRAVERSAL_SPECIALS",
    "ScriptContext",
    "TRAVERSAL_SPECIALS",
    "interpret",
]
