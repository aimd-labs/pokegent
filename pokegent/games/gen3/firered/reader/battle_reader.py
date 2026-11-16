"""Battle state reader: ``BattleBlock`` from one atomic memory image (spec 4.3 ``battle`` block).

``BattleReader(port, profile).read(ui)`` is a pure function of the memory image plus the UI evidence
that was read from the SAME image (the evidence holds the ``inBattle`` gate and the controller
table; this reader adds the battler structs and the battle-script engine state).

Gate: nothing of the battle RAM is exposed unless ``ui.main.in_battle`` is true. Outside a battle
the block is ``BattleBlock(active=False)`` and holds no battler (the RAM is stale there, m21 trap
3).

``BattleSnapshot`` carries the block next to the core snapshot without touching the frozen
``StateSnapshot`` schema (``state_hash`` is unchanged), and ``BattleSnapshotSource`` is the
``SnapshotSource`` the runner needs: ONE atomic snapshot feeds the core reader, the UI evidence
reader and this reader. The integrator may later promote ``battle`` into ``StateSnapshot`` itself.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

from pokegent.actions.battle import state as S
from pokegent.actions.battle.lookups import collect_lookups
from pokegent.actions.battle.romtables import RomTables
from pokegent.actions.battle.symbols import resolve
from pokegent.games.gen3.firered.reader.live import (
    AtomicSnapshotPort,
    AtomicSnapshotSource,
    LiveReader,
)
from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    ROM_START,
    MemoryPort,
    MemoryReadError,
)
from pokegent.games.gen3.firered.reader.reader import PartySource
from pokegent.games.gen3.firered.reader.schemas import StateSnapshot
from pokegent.games.gen3.firered.reader.ui_schemas import UiEvidence
from pokegent.games.gen3.firered.rom.rom_data import RomProfile
from pokegent.games.gen3.firered.rom.text import EOS, NEWLINE, WESTERN_CHARMAP
from pokegent.games.gen3.firered.rom.trainer import (
    TRAINER_COUNT,
    TRAINER_ENTRY_SIZE,
    TrainerRomError,
    TrainerTable,
)
from pokegent.state.evidence import UiEvidenceReader

_ROM_END = 0x0A00_0000

# Text control codes (include/characters / charmap.txt): argument byte counts after 0xFC.
_FC_ARGS = {0x01: 1, 0x02: 1, 0x03: 1, 0x04: 3, 0x05: 1, 0x06: 1, 0x07: 0, 0x08: 1, 0x09: 0,
            0x0A: 1, 0x0B: 2, 0x0C: 1, 0x0D: 1, 0x0E: 1, 0x0F: 0, 0x10: 2}  # fmt: skip


def decode_battle_string(raw: bytes) -> str:
    """The player-visible text of a battle string buffer (control codes dropped, paragraphs kept).

    ``0xFA`` is a scroll, ``0xFB`` a paragraph, ``0xFE`` a line break, ``0xFC n..`` a control code
    with arguments (colour, pause, ...), ``0xFD n`` a placeholder that was already expanded. The
    buffer is expected to be terminated by ``0xFF``; stale bytes after it are ignored.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == EOS:
            break
        if b == NEWLINE:
            out.append("\n")
        elif b == 0xFA:
            out.append(" ")
        elif b == 0xFB:
            out.append("\n\n")
        elif b == 0xFC and i + 1 < len(raw):
            i += 1 + _FC_ARGS.get(raw[i + 1], 0)
        elif b == 0xFD:
            i += 1
        elif b in WESTERN_CHARMAP:
            out.append(WESTERN_CHARMAP[b])
        else:
            out.append("?")
        i += 1
    return "".join(out).strip()


class BattleReader:
    def __init__(self, port: MemoryPort, profile: RomProfile) -> None:
        self._port = port
        self._profile = profile

    # -- helpers ----------------------------------------------------------------------------

    def _addr(self, name: str) -> int | None:
        return resolve(self._profile, name)

    def _read(self, errors: list[str], what: str, addr: int | None, size: int) -> bytes | None:
        if addr is None:
            errors.append(f"{what}: symbol missing from the profile")
            return None
        try:
            return self._port.read_range(addr, size)
        except (MemoryReadError, ValueError) as exc:
            errors.append(f"{what}: {exc}")
            return None

    # -- public -----------------------------------------------------------------------------

    def read(self, ui: UiEvidence | None, core: StateSnapshot | None = None) -> S.BattleBlock:
        """The block. ``core`` (the same moment's core snapshot) lets the reader capture the ROM
        lookups for the party and bag; without it the lookups are empty."""
        if ui is None or not ui.valid or ui.main.in_battle is None:
            return S.BattleBlock(
                active=False, valid=False, read_errors=("battle gate unknown: no UI evidence",)
            )
        if not ui.main.in_battle:
            return S.BattleBlock(active=False)
        ev = ui.battle
        if ev is None or not ev.valid or ev.type_flags is None or ev.battlers_count is None:
            return S.BattleBlock(
                active=True, valid=False, read_errors=("ui.battle evidence missing or invalid",)
            )

        errors: list[str] = []
        count = ev.battlers_count
        if not 1 <= count <= S.MAX_BATTLERS:
            return S.BattleBlock(
                active=True, valid=False, read_errors=(f"battlers count {count} out of range",)
            )

        # 1. battler structs ------------------------------------------------------------------
        mons = self._read(
            errors, "gBattleMons", self._addr("gBattleMons"), S.BM_SIZE * S.MAX_BATTLERS
        )
        # 2. per-battler words: gBattlerAttacker .. gBattleOutcome in one read
        a0 = self._addr("gLastUsedItem")
        a1 = self._addr("gBattleOutcome")
        words = None
        if a0 is not None and a1 is not None:
            words = self._read(errors, "battle words", a0, a1 + 1 - a0)
        # 3. weather .. gMoveToLearn in one read
        w0 = self._addr("gBattleWeather")
        t1 = self._addr("gMoveToLearn")
        tail = None
        if w0 is not None and t1 is not None:
            tail = self._read(errors, "battle tail", w0, t1 + 2 - w0)
        # 4. misc
        absent = self._read(errors, "gAbsentBattlerFlags", self._addr("gAbsentBattlerFlags"), 1)
        sides = self._sides()
        msg_raw = self._read(
            errors, "gDisplayedStringBattle", self._addr("gDisplayedStringBattle"), 300
        )
        main_func = self._read(errors, "gBattleMainFunc", self._addr("gBattleMainFunc"), 4)
        results = self._read(errors, "gBattleResults", self._addr("gBattleResults"), 0x14)
        n_moves = self._read(
            errors, "gNumberOfMovesToChoose", self._addr("gNumberOfMovesToChoose"), 1
        )
        script = self._read(
            errors, "gBattlescriptCurrInstr", self._addr("gBattlescriptCurrInstr"), 4
        )
        if mons is None or words is None or tail is None or absent is None:
            return S.BattleBlock(
                active=True, valid=False, read_errors=tuple(errors), type_flags=ev.type_flags
            )

        def off(name: str, base: int) -> int:
            addr = self._addr(name)
            assert addr is not None
            return addr - base

        # per-battler words
        base = a0  # type: ignore[assignment]
        last_moves = struct.unpack_from("<4H", words, off("gLastMoves", base))
        chosen_moves = struct.unpack_from("<4H", words, off("gChosenMoveByBattler", base))
        co = off("gChosenActionByBattler", base)
        chosen_actions = tuple(words[co : co + 4])
        statuses3 = struct.unpack_from("<4I", words, off("gStatuses3", base))
        ds_off = off("gDisableStructs", base)
        disables = [
            S.decode_disable_struct(words[ds_off + i * S.DS_SIZE : ds_off + (i + 1) * S.DS_SIZE])
            for i in range(S.MAX_BATTLERS)
        ]
        last_used_item = struct.unpack_from("<H", words, 0)[0]
        turn_marker = struct.unpack_from("<H", words, off("gRandomTurnNumber", base))[0]
        comm = tuple(
            words[off("gBattleCommunication", base) : off("gBattleCommunication", base) + 8]
        )
        outcome = words[off("gBattleOutcome", base)]

        # BattleStruct via its pointer (choice lock)
        choiced = (0, 0, 0, 0)
        move_targets = (0, 0, 0, 0)
        safari_rock = safari_bait = safari_escape = safari_catch = 0
        run_tries = 0
        learn_move_party_id = 0
        bs_ptr = struct.unpack_from("<I", tail, off("gBattleStruct", w0))[0]  # type: ignore[arg-type]
        if EWRAM_START <= bs_ptr and bs_ptr % 4 == 0 and bs_ptr + S.BS_READ_SIZE <= EWRAM_END:
            bs = self._read(errors, "gBattleStruct", bs_ptr, S.BS_READ_SIZE)
            if bs is not None:
                choiced = struct.unpack_from("<4H", bs, S.BS_CHOICED_MOVE)
                move_targets = tuple(bs[S.BS_MOVE_TARGET : S.BS_MOVE_TARGET + S.MAX_BATTLERS])
                safari_rock = bs[S.BS_SAFARI_ROCK_COUNTER]
                safari_bait = bs[S.BS_SAFARI_BAIT_COUNTER]
                safari_escape = bs[S.BS_SAFARI_ESCAPE_FACTOR]
                safari_catch = bs[S.BS_SAFARI_CATCH_FACTOR]
                run_tries = bs[S.BS_RUN_TRIES]
                learn_move_party_id = bs[S.BS_EXP_GETTER_MON_ID]

        scripting = off("gBattleScripting", w0)  # type: ignore[arg-type]
        lvlup_state = tail[scripting + S.SCR_LVLUPBOX_STATE]
        learn_state = tail[scripting + S.SCR_LEARNMOVE_STATE]
        battle_style = tail[scripting + S.SCR_BATTLE_STYLE]
        weather = struct.unpack_from("<H", tail, 0)[0]
        move_to_learn = struct.unpack_from("<H", tail, off("gMoveToLearn", w0))[0]  # type: ignore[arg-type]

        absent_flags = absent[0]
        battlers: list[S.Battler] = []
        for b in range(count):
            fields = S.decode_battle_mon(mons[b * S.BM_SIZE : (b + 1) * S.BM_SIZE])
            battlers.append(
                S.Battler(
                    battler=b,
                    side="opponent" if b & 1 else "player",
                    present=not bool(absent_flags & (1 << b)),
                    status3=statuses3[b],
                    disable=disables[b],
                    last_move=last_moves[b],
                    chosen_move=chosen_moves[b],
                    chosen_action=chosen_actions[b],
                    choiced_move=choiced[b],
                    **fields,
                )
            )

        # script engine
        script_ptr = struct.unpack("<I", script)[0] if script else 0
        script_op = None
        if ROM_START <= script_ptr < _ROM_END:
            try:
                script_op = self._port.read_range(script_ptr, 1)[0]
            except (MemoryReadError, ValueError):
                script_op = None
        dex_registration_state = (
            self._dex_registration_state() if script_op == S.OP_DISPLAYDEXINFO else None
        )
        main_ptr = struct.unpack("<I", main_func)[0] if main_func else 0

        controller0 = next((c for c in ev.controllers if c.battler == 0), None)
        kind, reason, supported = S.classify_battle(
            ev.type_flags, player_variant=controller0.variant if controller0 else None
        )

        enemy_party = self._enemy_party(errors) if kind in ("trainer", "wild") else None
        opponent_trainer = self._opponent_trainer(errors) if kind == "trainer" else None
        safari_balls = None
        if kind == "safari":
            safari_raw = self._read(errors, "gNumSafariBalls", self._addr("gNumSafariBalls"), 1)
            safari_balls = safari_raw[0] if safari_raw else None
        message = decode_battle_string(msg_raw) if msg_raw else ""
        lookups = self._lookups(battlers, core)

        return S.BattleBlock(
            active=True,
            valid=True,
            read_errors=tuple(errors),
            type_flags=ev.type_flags,
            kind=kind,
            kind_reason=reason,
            supported=supported,
            battlers_count=count,
            battlers=tuple(battlers),
            active_battler=ev.active_battler,
            input_battlers=tuple(ev.input_battlers),
            party_indexes=tuple(ev.party_indexes[:count]),
            move_targets=move_targets,
            absent_flags=absent_flags,
            positions=tuple(ev.battler_positions[:count]),
            enemy_party=enemy_party,
            opponent_trainer=opponent_trainer,
            weather=S.weather_names(weather),
            sides=sides,
            outcome=outcome,
            script_ptr=script_ptr,
            script_op=script_op,
            dex_registration_state=dex_registration_state,
            main_func=main_ptr,
            comm=comm,
            lvlup_state=lvlup_state,
            learn_move_state=learn_state,
            battle_style=battle_style,
            move_to_learn=move_to_learn,
            learn_move_party_id=learn_move_party_id,
            number_of_moves=n_moves[0] if n_moves else 0,
            message=message,
            lookups=lookups,
            last_used_item=last_used_item,
            safari_rock_turns=safari_rock,
            safari_bait_turns=safari_bait,
            safari_balls=safari_balls,
            safari_escape_factor=safari_escape,
            safari_catch_factor=safari_catch,
            run_tries=run_tries,
            turn_marker=turn_marker,
            turn=results[0x13] if results else 0,
        )

    def _dex_registration_state(self) -> int | None:
        """Pret ``PokedexScreenData.state`` for the caught-mon registration task, if live."""
        pointer_address = self._addr("sPokedexScreenData")
        if pointer_address is None:
            return None
        try:
            pointer = self._port.read_u32(pointer_address)
            if not (EWRAM_START <= pointer < EWRAM_END):
                return None
            return self._port.read_u8(pointer + 1)
        except (MemoryReadError, ValueError):
            return None

    def _sides(self) -> tuple[S.SideInfo, S.SideInfo] | None:
        """Screens, spikes, safeguard, mist per side; ``None`` if the profile lacks the symbols or
        the read fails (the mechanics adapter then widens its damage ranges instead)."""
        a_status, a_timers = self._addr("gSideStatuses"), self._addr("gSideTimers")
        if a_status is None or a_timers is None:
            return None
        try:
            return S.decode_sides(
                self._port.read_range(a_status, 4), self._port.read_range(a_timers, 24)
            )
        except (MemoryReadError, ValueError):
            return None

    def _lookups(self, battlers: list[S.Battler], core: StateSnapshot | None) -> S.BattleLookups:
        species = {b.species for b in battlers}
        moves = {m.move_id for b in battlers if b.side == "player" for m in b.moves}
        abilities = {b.ability for b in battlers if b.side == "player"}
        items = {b.item for b in battlers if b.side == "player"}
        if core is not None and core.usable:
            for mon in core.party:
                species.add(mon.species)
                moves.update(m.move_id for m in mon.moves)
                items.add(mon.held_item)
            for pocket in core.bag:
                items.update(i.item_id for i in pocket.items)
        return collect_lookups(
            RomTables(self._port, self._profile, use_generated=True),
            species=species,
            abilities=abilities,
            moves=moves,
            items=items,
        )

    def _enemy_party(self, errors: list[str]) -> S.EnemyPartyInfo | None:
        raw = self._read(errors, "gEnemyParty", self._addr("gEnemyParty"), 600)
        if raw is None:
            return None
        size = alive = 0
        for slot in range(6):
            mon = raw[slot * 100 : (slot + 1) * 100]
            level = mon[0x54]
            if level == 0:
                continue
            size += 1
            if struct.unpack_from("<H", mon, 0x56)[0] > 0:
                alive += 1
        return S.EnemyPartyInfo(size=size, alive=alive)

    def _opponent_trainer(self, errors: list[str]) -> S.OpponentTrainer:
        raw_id = self._read(
            errors, "gTrainerBattleOpponent_A", self._addr("gTrainerBattleOpponent_A"), 2
        )
        if raw_id is None:
            return S.OpponentTrainer()
        trainer_id = struct.unpack_from("<H", raw_id)[0]
        if not 0 < trainer_id < TRAINER_COUNT:
            return S.OpponentTrainer()
        table = self._profile.rom_table("gTrainers")
        class_table = self._profile.rom_table("gTrainerClassNames")
        if table is None or class_table is None:
            errors.append("gTrainers: table missing from the profile")
            return S.OpponentTrainer(trainer_id=trainer_id)
        try:
            if trainer_id >= table.size // TRAINER_ENTRY_SIZE:
                return S.OpponentTrainer()
            row = self._port.read_range(
                table.address + trainer_id * TRAINER_ENTRY_SIZE, TRAINER_ENTRY_SIZE
            )
            class_rows = self._port.read_range(class_table.address, class_table.size)
            decoded_id, class_name, name = TrainerTable.metadata_from_record(
                trainer_id, row, class_rows
            )
            return S.OpponentTrainer(
                trainer_id=decoded_id, class_name=class_name, name=name or "unknown"
            )
        except (MemoryReadError, TrainerRomError, ValueError):
            return S.OpponentTrainer(trainer_id=trainer_id)


# -- snapshot carrying the block ----------------------------------------------------------------


class BattleSnapshot(StateSnapshot):
    """A :class:`StateSnapshot` that also carries the battle block.

    Built with :meth:`attach` (no re-validation, ``state_hash`` untouched). Plain snapshots stay
    valid inputs everywhere; detectors and builders read ``getattr(snapshot, "battle", None)``.
    """

    battle: S.BattleBlock | None = None

    @classmethod
    def attach(cls, snapshot: StateSnapshot, block: S.BattleBlock | None) -> BattleSnapshot:
        values: dict[str, Any] = {
            name: getattr(snapshot, name) for name in StateSnapshot.model_fields
        }
        return cls.model_construct(**values, battle=block)


def battle_of(snapshot: StateSnapshot) -> S.BattleBlock | None:
    return getattr(snapshot, "battle", None)


class BattleSnapshotSource:
    """``SnapshotSource`` for the battle family: core + UI + battle block from one atomic image."""

    def __init__(
        self,
        source: AtomicSnapshotSource,
        profile: RomProfile,
        *,
        run_id: str = "",
        party_source: PartySource = "live",
        on_read: Callable[[BattleSnapshot], None] | None = None,
    ) -> None:
        self._source = source
        self._profile = profile
        self._live = LiveReader(source, profile, run_id=run_id, party_source=party_source)
        self._on_read = on_read

    def read_snapshot(self) -> StateSnapshot:
        atomic = self._source.read_atomic_snapshot()
        port = AtomicSnapshotPort(atomic)
        core = self._live.read(atomic).snapshot
        ui = UiEvidenceReader(port, self._profile).read()
        snapshot = core.with_ui(ui)
        block = BattleReader(port, self._profile).read(ui, snapshot)
        out = BattleSnapshot.attach(snapshot, block)
        if self._on_read is not None:
            self._on_read(out)
        return out
