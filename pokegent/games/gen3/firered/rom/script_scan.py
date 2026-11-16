"""A minimal, read-only decoder for the game's event scripts (``script`` bytecode in the ROM).

Why it exists: facts about quests (who gives an item, which flag proves it, what has to be true
first)
must be checked against the ROM, not copied from a wiki or a decompile that may differ per revision.
The E4 defeated flags were verified by scanning ``setflag`` bytes; this module does the same one
level
up: it follows a person's script from the object event's script pointer through ``goto`` /
``call`` /
``goto_if`` / ``call_if`` and trainerbattle continuation scripts, and lists every command it can
reach, so a test can say "this script checks
flag 0x241, sets it, and adds item 352".

Not an interpreter: nothing is executed, conditions are not evaluated, ``callstd`` targets are not
followed (the standard scripts are the same for everyone). An opcode whose length depends on its
operands (``trainerbattle`` and some of the ``buffer*`` family) is not in the table and raises
:class:`UnknownOpcode`, so a script that needs them fails loudly instead of being decoded wrongly.

``OPS`` is generated from pret/pokefirered (commit 037335f, ``data/script_cmd_table.inc`` for
the opcode,
``asm/macros/event.inc`` for the operand bytes of each command); the decode of the scripts in
``local script overlays`` is checked against the ROM by ``historical local validation`` (every
followed branch must end in ``end`` / ``return`` / ``goto`` / ``gotostd`` and every text pointer
must
decode), which is what verifies the table for the commands those scripts use.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

ROM_BASE = 0x0800_0000

#: opcode -> (name, operand bytes after the opcode)
OPS: dict[int, tuple[str, int]] = {
    0x00: ("nop", 0),
    0x01: ("nop1", 0),
    0x02: ("end", 0),
    0x03: ("return", 0),
    0x04: ("call", 4),
    0x05: ("goto", 4),
    0x06: ("goto_if", 5),
    0x07: ("call_if", 5),
    0x08: ("gotostd", 1),
    0x09: ("callstd", 1),
    0x0A: ("gotostd_if", 2),
    0x0B: ("callstd_if", 2),
    0x0C: ("returnram", 0),
    0x0D: ("endram", 0),
    0x0E: ("setmysteryeventstatus", 1),
    0x0F: ("loadword", 5),
    0x10: ("loadbyte", 2),
    0x11: ("setptr", 5),
    0x12: ("loadbytefromptr", 5),
    0x13: ("setptrbyte", 5),
    0x14: ("copylocal", 2),
    0x15: ("copybyte", 8),
    0x16: ("setvar", 4),
    0x17: ("addvar", 4),
    0x18: ("subvar", 4),
    0x19: ("copyvar", 4),
    0x1A: ("setorcopyvar", 4),
    0x1B: ("compare_local_to_local", 2),
    0x1C: ("compare_local_to_value", 2),
    0x1D: ("compare_local_to_ptr", 5),
    0x1E: ("compare_ptr_to_local", 5),
    0x1F: ("compare_ptr_to_value", 5),
    0x20: ("compare_ptr_to_ptr", 8),
    0x21: ("compare_var_to_value", 4),
    0x22: ("compare_var_to_var", 4),
    0x23: ("callnative", 4),
    0x24: ("gotonative", 4),
    0x25: ("special", 2),
    0x26: ("specialvar", 4),
    0x27: ("waitstate", 0),
    0x28: ("delay", 2),
    0x29: ("setflag", 2),
    0x2A: ("clearflag", 2),
    0x2B: ("checkflag", 2),
    0x2C: ("initclock", 4),
    0x2D: ("dotimebasedevents", 0),
    0x2E: ("gettime", 0),
    0x2F: ("playse", 2),
    0x30: ("waitse", 0),
    0x31: ("playfanfare", 2),
    0x32: ("waitfanfare", 0),
    0x33: ("playbgm", 3),
    0x34: ("savebgm", 2),
    0x35: ("fadedefaultbgm", 0),
    0x36: ("fadenewbgm", 2),
    0x37: ("fadeoutbgm", 1),
    0x38: ("fadeinbgm", 1),
    0x39: ("warp", 7),
    0x3A: ("warpsilent", 7),
    0x3B: ("warpdoor", 7),
    0x3C: ("warphole", 2),
    0x3D: ("warpteleport", 7),
    0x3E: ("setwarp", 7),
    0x3F: ("setdynamicwarp", 7),
    0x42: ("getplayerxy", 4),
    0x43: ("getpartysize", 0),
    0x44: ("additem", 4),
    0x45: ("removeitem", 4),
    0x46: ("checkitemspace", 4),
    0x47: ("checkitem", 4),
    0x48: ("checkitemtype", 2),
    0x49: ("addpcitem", 4),
    0x4A: ("checkpcitem", 4),
    0x4B: ("adddecoration", 2),
    0x4C: ("removedecoration", 2),
    0x4D: ("checkdecor", 2),
    0x4E: ("checkdecorspace", 2),
    0x4F: ("applymovement", 6),
    0x50: ("applymovementat", 8),
    0x51: ("waitmovement", 2),
    0x52: ("waitmovementat", 4),
    0x53: ("removeobject", 2),
    0x54: ("removeobjectat", 4),
    0x55: ("addobject", 2),
    0x56: ("addobjectat", 4),
    0x57: ("setobjectxy", 6),
    0x58: ("showobjectat", 4),
    0x59: ("hideobjectat", 4),
    0x5A: ("faceplayer", 0),
    0x5B: ("turnobject", 3),
    0x5D: ("dotrainerbattle", 0),
    0x5E: ("gotopostbattlescript", 0),
    0x5F: ("gotobeatenscript", 0),
    0x60: ("checktrainerflag", 2),
    0x61: ("settrainerflag", 2),
    0x62: ("cleartrainerflag", 2),
    0x63: ("setobjectxyperm", 6),
    0x64: ("copyobjectxytoperm", 2),
    0x65: ("setobjectmovementtype", 3),
    0x66: ("waitmessage", 0),
    0x67: ("message", 4),
    0x68: ("closemessage", 0),
    0x69: ("lockall", 0),
    0x6A: ("lock", 0),
    0x6B: ("releaseall", 0),
    0x6C: ("release", 0),
    0x6D: ("waitbuttonpress", 0),
    0x6E: ("yesnobox", 2),
    0x6F: ("multichoice", 4),
    0x70: ("multichoicedefault", 5),
    0x71: ("multichoicegrid", 5),
    0x72: ("drawbox", 0),
    0x73: ("erasebox", 4),
    0x74: ("drawboxtext", 4),
    0x75: ("showmonpic", 4),
    0x76: ("hidemonpic", 0),
    0x77: ("showcontestpainting", 1),
    0x78: ("braillemessage", 4),
    0x79: ("givemon", 14),
    0x7A: ("giveegg", 2),
    0x7B: ("setmonmove", 4),
    0x7C: ("checkpartymove", 2),
    0x7D: ("bufferspeciesname", 3),
    0x7E: ("bufferleadmonspeciesname", 1),
    0x7F: ("bufferpartymonnick", 3),
    0x80: ("bufferitemname", 3),
    0x81: ("bufferdecorationname", 3),
    0x82: ("buffermovename", 3),
    0x83: ("buffernumberstring", 3),
    0x84: ("bufferstdstring", 3),
    0x85: ("bufferstring", 5),
    0x86: ("pokemart", 4),
    0x87: ("pokemartdecoration", 4),
    0x88: ("pokemartdecoration2", 4),
    0x89: ("playslotmachine", 2),
    0x8A: ("setberrytree", 3),
    0x8B: ("choosecontestmon", 0),
    0x8C: ("startcontest", 0),
    0x8D: ("showcontestresults", 0),
    0x8E: ("contestlinktransfer", 0),
    0x8F: ("random", 2),
    0x90: ("addmoney", 5),
    0x91: ("removemoney", 5),
    0x92: ("checkmoney", 5),
    0x93: ("showmoneybox", 3),
    0x94: ("hidemoneybox", 2),
    0x95: ("updatemoneybox", 3),
    0x96: ("getpokenewsactive", 2),
    0x97: ("fadescreen", 1),
    0x98: ("fadescreenspeed", 2),
    0x99: ("setflashlevel", 2),
    0x9A: ("animateflash", 1),
    0x9B: ("messageautoscroll", 4),
    0x9C: ("dofieldeffect", 2),
    0x9D: ("setfieldeffectargument", 3),
    0x9E: ("waitfieldeffect", 2),
    0x9F: ("setrespawn", 2),
    0xA0: ("checkplayergender", 0),
    0xA1: ("playmoncry", 4),
    0xA2: ("setmetatile", 8),
    0xA3: ("resetweather", 0),
    0xA4: ("setweather", 2),
    0xA5: ("doweather", 0),
    0xA6: ("setstepcallback", 1),
    0xA7: ("setmaplayoutindex", 2),
    0xAA: ("createvobject", 8),
    0xAB: ("turnvobject", 2),
    0xAC: ("opendoor", 4),
    0xAD: ("closedoor", 4),
    0xAE: ("waitdooranim", 0),
    0xAF: ("setdooropen", 4),
    0xB0: ("setdoorclosed", 4),
    0xB1: ("addelevmenuitem", 7),
    0xB2: ("showelevmenu", 0),
    0xB3: ("checkcoins", 2),
    0xB4: ("addcoins", 2),
    0xB5: ("removecoins", 2),
    0xB6: ("setwildbattle", 5),
    0xB7: ("dowildbattle", 0),
    0xB8: ("setvaddress", 4),
    0xB9: ("vgoto", 4),
    0xBA: ("vcall", 4),
    0xBB: ("vgoto_if", 5),
    0xBC: ("vcall_if", 5),
    0xBD: ("vmessage", 4),
    0xBE: ("vbuffermessage", 4),
    0xBF: ("vbufferstring", 5),
    0xC0: ("showcoinsbox", 2),
    0xC1: ("hidecoinsbox", 2),
    0xC2: ("updatecoinsbox", 2),
    0xC3: ("incrementgamestat", 1),
    0xC4: ("setescapewarp", 7),
    0xC5: ("waitmoncry", 0),
    0xC6: ("bufferboxname", 3),
    0xC7: ("textcolor", 1),
    0xC8: ("loadhelp", 4),
    0xC9: ("unloadhelp", 0),
    0xCA: ("signmsg", 0),
    0xCB: ("normalmsg", 0),
    0xCC: ("comparestat", 5),
    0xCD: ("setmonmodernfatefulencounter", 2),
    0xCE: ("checkmonmodernfatefulencounter", 2),
    0xCF: ("trywondercardscript", 0),
    0xD0: ("setworldmapflag", 2),
    0xD1: ("warpspinenter", 7),
    0xD2: ("setmonmetlocation", 3),
    0xD3: ("getbraillestringwidth", 4),
    0xD4: ("bufferitemnameplural", 5),
}

#: commands that end a linear stream
_TERMINAL = frozenset({"end", "return", "goto", "gotostd", "endram", "returnram", "gotonative"})
#: commands whose last operand is a ROM pointer to more script
_BRANCH = frozenset({"goto", "call", "goto_if", "call_if"})

# TRAINER_BATTLE_CONTINUE_SCRIPT[_NO_MUSIC] and the double variants carry an
# event-script pointer as their final operand.  Text pointers in the other
# trainerbattle forms are deliberately not followed.
_TRAINER_SCRIPT_TYPES = frozenset({1, 2, 6, 8})

# condition operands of goto_if / call_if (pret: LT 0, EQ 1, GT 2, LE 3, GE 4, NE 5)
CONDITIONS = {0: "lt", 1: "eq", 2: "gt", 3: "le", 4: "ge", 5: "ne"}


class UnknownOpcode(ValueError):
    """The decoder met an opcode it cannot size (or ran off the ROM)."""


@dataclass(frozen=True)
class Cmd:
    addr: int  # ROM offset of the opcode
    op: int
    name: str
    operands: bytes

    def u16(self, at: int = 0) -> int:
        return int.from_bytes(self.operands[at : at + 2], "little")

    def u32(self, at: int = 0) -> int:
        return int.from_bytes(self.operands[at : at + 4], "little")

    @property
    def target(self) -> int | None:
        """ROM offset a branch command jumps to."""
        if self.name not in _BRANCH:
            return None
        pointer = int.from_bytes(self.operands[-4:], "little")
        return pointer - ROM_BASE if ROM_BASE <= pointer < ROM_BASE + 0x0200_0000 else None

    @property
    def trainer_script_target(self) -> int | None:
        """ROM offset of a trainerbattle command's post-battle script."""
        if self.name != "trainerbattle" or not self.operands:
            return None
        if self.operands[0] not in _TRAINER_SCRIPT_TYPES or len(self.operands) < 5:
            return None
        pointer = self.u32(len(self.operands) - 4)
        return pointer - ROM_BASE if ROM_BASE <= pointer < ROM_BASE + 0x0200_0000 else None

    def __str__(self) -> str:
        return f"{self.addr:06X} {self.name} {self.operands.hex(' ')}"


def decode_command(rom: bytes, offset: int) -> Cmd:
    """Decode one command at a ROM offset."""
    if not 0 <= offset < len(rom):
        raise UnknownOpcode(f"script ran off the ROM at {offset:#x}")
    op = rom[offset]
    spec = OPS.get(op)
    # ``trainerbattle`` is the only command used by the world adapter whose size depends on
    # an operand.  The ten variants are fixed by constants/battle_setup.h and
    # asm/macros/event.inc.  Keeping this here lets the ordinary branch walker continue past
    # trainer objects instead of treating their scripts as opaque bytes.
    if op == 0x5C and offset + 2 <= len(rom):
        battle_type = rom[offset + 1]
        sizes = {0: 13, 1: 17, 2: 17, 3: 9, 4: 17, 5: 13, 6: 21, 7: 17, 8: 21, 9: 13}
        size = sizes.get(battle_type)
        spec = ("trainerbattle", size) if size is not None else None
    if spec is None:
        raise UnknownOpcode(f"opcode {op:#04x} at {offset:#08x} has no known length")
    name, size = spec
    end = offset + 1 + size
    if end > len(rom):
        raise UnknownOpcode(f"command {name} at {offset:#08x} runs off the ROM")
    return Cmd(offset, op, name, bytes(rom[offset + 1 : end]))


def decode_stream(rom: bytes, offset: int, *, limit: int = 400) -> list[Cmd]:
    """Decode one linear stream from ``offset`` to its terminal command (inclusive)."""
    cmds: list[Cmd] = []
    at = offset
    while len(cmds) < limit:
        cmd = decode_command(rom, at)
        cmds.append(cmd)
        at += 1 + len(cmd.operands)
        if cmd.name in _TERMINAL:
            return cmds
    raise UnknownOpcode(f"no terminal command within {limit} commands from {offset:#x}")


def reachable(rom: bytes, script_ptr: int, *, max_streams: int = 64) -> list[Cmd]:
    """Every command reachable through branches or trainerbattle post-battle scripts."""
    offset = script_ptr - ROM_BASE if script_ptr >= ROM_BASE else script_ptr
    seen: set[int] = set()
    todo = [offset]
    out: list[Cmd] = []
    while todo:
        start = todo.pop(0)
        if start in seen:
            continue
        seen.add(start)
        if len(seen) > max_streams:
            raise UnknownOpcode(f"more than {max_streams} streams from {script_ptr:#x}")
        stream = decode_stream(rom, start)
        out.extend(stream)
        todo.extend(
            target
            for c in stream
            for target in (c.target, c.trainer_script_target)
            if target is not None
        )
    return out


def text_pointers(cmds: list[Cmd]) -> Iterator[int]:
    """ROM offsets of the strings ``msgbox`` / ``message`` load (``loadword 0, <text>``)."""
    for c in cmds:
        if c.name == "loadword" and c.operands[0] == 0:
            pointer = c.u32(1)
            if ROM_BASE <= pointer < ROM_BASE + 0x0200_0000:
                yield pointer - ROM_BASE
        elif c.name == "message":
            pointer = c.u32()
            if ROM_BASE <= pointer < ROM_BASE + 0x0200_0000:
                yield pointer - ROM_BASE


def flags_checked(cmds: list[Cmd]) -> list[int]:
    return [c.u16() for c in cmds if c.name == "checkflag"]


def flags_set(cmds: list[Cmd]) -> list[int]:
    return [c.u16() for c in cmds if c.name == "setflag"]


def flags_cleared(cmds: list[Cmd]) -> list[int]:
    return [c.u16() for c in cmds if c.name == "clearflag"]


def items_added(cmds: list[Cmd]) -> list[tuple[int, int]]:
    """``additem item, quantity`` as ``(item, quantity)``."""
    return [(c.u16(), c.u16(2)) for c in cmds if c.name == "additem"]


def items_removed(cmds: list[Cmd]) -> list[tuple[int, int]]:
    return [(c.u16(), c.u16(2)) for c in cmds if c.name == "removeitem"]
