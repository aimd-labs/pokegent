"""ROM profiles and ROM identification (spec 4.1).

A *profile* is a named, source-annotated set of symbolic addresses and struct field offsets
for one exact ROM build. ``references/rom_profiles.yaml`` is the single source of truth.

Identification rules:

* A ROM matches a profile only if its sha1 matches, the recorded sha256 matches (when the
  profile records one), the header game code (``0xAC``) and version byte (``0xBC``) match, and
  the profile is marked ``supported``. The file extension is never consulted.
* Anything else is a typed ``UNSUPPORTED_ROM`` result with the reasons spelled out. A missing
  ROM is ``AWAITING_ROM`` (spec 13.1), never a silent pass. No address is ever guessed for an
  unknown build.
* The ROM file is opened read-only and is never copied or written.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokegent.games.gen3.firered.reader.memory_port import (
    EWRAM_END,
    EWRAM_START,
    IWRAM_END,
    IWRAM_START,
    ROM_START,
)

ROM_PATH_ENV = "FIRERED_ROM_PATH"
PROFILES_PATH_ENV = "POKEGENT_ROM_PROFILES"
HEADER_GAME_CODE_OFFSET = 0xAC
HEADER_VERSION_OFFSET = 0xBC
HEADER_SIZE = 0xC0
_CHUNK = 1 << 20


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfileError(Exception):
    """The profile file is malformed, or a profile cannot provide what the caller needs."""


class UnsupportedRomError(Exception):
    """Raised by callers that require a supported ROM; carries the typed identification."""

    def __init__(self, identification: RomIdentification) -> None:
        super().__init__(f"{identification.status.value}: " + "; ".join(identification.reasons))
        self.identification = identification


class SymbolEntry(_Frozen):
    address: int
    size: int | None = None
    source: str = Field(min_length=1)
    verified_by: tuple[str, ...] = ()

    @property
    def region(self) -> Literal["EWRAM", "IWRAM", "ROM"]:
        if EWRAM_START <= self.address < EWRAM_END:
            return "EWRAM"
        if IWRAM_START <= self.address < IWRAM_END:
            return "IWRAM"
        return "ROM"

    @model_validator(mode="after")
    def _check_region(self) -> SymbolEntry:
        end = self.address + (self.size or 1)
        in_ewram = EWRAM_START <= self.address and end <= EWRAM_END
        in_iwram = IWRAM_START <= self.address and end <= IWRAM_END
        in_rom = self.address >= ROM_START
        if not (in_ewram or in_iwram or in_rom):
            raise ValueError(
                f"symbol range 0x{self.address:08X}+{self.size} is outside GBA RAM/ROM"
            )
        return self


class RomTableEntry(_Frozen):
    """A ROM DATA table (names, move/species/item rows ...). Kept apart from ``symbols`` because
    sized ROM symbols there are checked as data and unsized ones as Thumb function entry points
    (historical local validation, test_evidence_rom.py)."""

    address: int
    size: int = Field(gt=0)
    source: str = Field(min_length=1)
    verified_by: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _in_rom(self) -> RomTableEntry:
        if not (ROM_START <= self.address and self.address + self.size <= 0x0A00_0000):
            raise ValueError(f"rom table range 0x{self.address:08X}+{self.size} is outside the ROM")
        return self


class FieldEntry(_Frozen):
    offset: int = Field(ge=0)
    size: int = Field(gt=0)
    source: str = Field(min_length=1)


class StructLayout(_Frozen):
    size: int = Field(gt=0)
    source: str = Field(min_length=1)
    fields: Mapping[str, FieldEntry]

    @model_validator(mode="after")
    def _fields_fit(self) -> StructLayout:
        for name, entry in self.fields.items():
            if entry.offset + entry.size > self.size:
                raise ValueError(f"field {name} ends past the struct size 0x{self.size:X}")
        return self


class RomIdentity(_Frozen):
    sha1: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    game_code: str = Field(min_length=4, max_length=4)
    version: int = Field(ge=0, le=255)
    size: int | None = None


class RomProfile(_Frozen):
    profile_id: str
    display_name: str
    supported: bool
    rom: RomIdentity
    layout_id: str | None = None
    structs: Mapping[str, StructLayout] = Field(default_factory=dict)
    symbols: Mapping[str, SymbolEntry] = Field(default_factory=dict)
    #: ROM data tables (``rom_tables`` section of the profile file), by decomp name
    rom_tables: Mapping[str, RomTableEntry] = Field(default_factory=dict)
    pret_commit: str | None = None

    @model_validator(mode="after")
    def _supported_needs_tables(self) -> RomProfile:
        if self.supported and not (self.structs and self.symbols):
            raise ValueError(f"profile {self.profile_id} is supported but has no layout/symbols")
        return self

    # -- lookups -----------------------------------------------------------------------

    def symbol(self, name: str) -> SymbolEntry:
        try:
            return self.symbols[name]
        except KeyError:
            raise ProfileError(f"profile {self.profile_id} has no symbol {name!r}") from None

    def has_symbol(self, name: str) -> bool:
        return name in self.symbols

    def rom_table(self, name: str) -> RomTableEntry:
        try:
            return self.rom_tables[name]
        except KeyError:
            raise ProfileError(f"profile {self.profile_id} has no ROM table {name!r}") from None

    def has_rom_table(self, name: str) -> bool:
        return name in self.rom_tables

    def field(self, struct: str, name: str) -> FieldEntry:
        try:
            return self.structs[struct].fields[name]
        except KeyError:
            raise ProfileError(f"profile {self.profile_id} has no field {struct}.{name}") from None

    def struct_size(self, struct: str) -> int:
        try:
            return self.structs[struct].size
        except KeyError:
            raise ProfileError(f"profile {self.profile_id} has no struct {struct!r}") from None


class RomStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_ROM = "UNSUPPORTED_ROM"
    AWAITING_ROM = "AWAITING_ROM"


class RomIdentification(_Frozen):
    """Typed outcome of ROM identification. ``profile_id`` is set only when SUPPORTED."""

    status: RomStatus
    profile_id: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    game_code: str | None = None
    version: int | None = None
    size: int | None = None
    reasons: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.status is RomStatus.SUPPORTED


class ProfileSet:
    """The loaded set of profiles, addressable by id or by sha1."""

    def __init__(self, profiles: list[RomProfile]) -> None:
        self._by_id: dict[str, RomProfile] = {}
        for profile in profiles:
            if profile.profile_id in self._by_id:
                raise ProfileError(f"duplicate profile id {profile.profile_id}")
            self._by_id[profile.profile_id] = profile

    def __iter__(self) -> Iterator[RomProfile]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, profile_id: str) -> RomProfile:
        try:
            return self._by_id[profile_id]
        except KeyError:
            raise ProfileError(f"unknown profile id {profile_id!r}") from None

    def by_sha1(self, sha1: str) -> list[RomProfile]:
        return [p for p in self._by_id.values() if p.rom.sha1 == sha1]


# -- loading ---------------------------------------------------------------------------


def default_profiles_path() -> Path:
    """Explicit override, then shipped structural metadata.

    Profiles describe layouts and addresses consumed by this exact code revision; they are
    hand-maintained decoder metadata, not extracted table contents.
    """
    override = os.environ.get(PROFILES_PATH_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[5] / "references" / "rom_profiles.yaml"


def parse_profiles(document: Mapping[str, object]) -> ProfileSet:
    """Build a :class:`ProfileSet` from a parsed YAML document (validated, fail-closed)."""
    if document.get("schema_version") != 1:
        raise ProfileError(
            f"unsupported rom_profiles schema_version {document.get('schema_version')!r}"
        )
    pret_commit = document.get("pret_commit")
    layouts = document.get("layouts") or {}
    profiles: list[RomProfile] = []
    raw_profiles = document.get("profiles") or {}
    if not isinstance(raw_profiles, dict) or not isinstance(layouts, dict):
        raise ProfileError("rom_profiles: 'profiles' and 'layouts' must be mappings")
    for profile_id, raw in raw_profiles.items():
        layout_id = raw.get("layout")
        structs: dict[str, StructLayout] = {}
        if layout_id is not None:
            if layout_id not in layouts:
                raise ProfileError(f"profile {profile_id} references unknown layout {layout_id!r}")
            structs = {
                name: StructLayout.model_validate(struct)
                for name, struct in layouts[layout_id]["structs"].items()
            }
        try:
            profiles.append(
                RomProfile(
                    profile_id=profile_id,
                    display_name=raw["display_name"],
                    supported=raw["supported"],
                    rom=RomIdentity.model_validate(raw["rom"]),
                    layout_id=layout_id,
                    structs=structs,
                    symbols={
                        name: SymbolEntry.model_validate(entry)
                        for name, entry in (raw.get("symbols") or {}).items()
                    },
                    rom_tables={
                        name: RomTableEntry.model_validate(entry)
                        for name, entry in (raw.get("rom_tables") or {}).items()
                    },
                    pret_commit=str(pret_commit) if pret_commit else None,
                )
            )
        except (KeyError, ValueError) as exc:
            raise ProfileError(f"profile {profile_id} is malformed: {exc}") from exc
    return ProfileSet(profiles)


def load_profiles(path: str | os.PathLike[str] | None = None) -> ProfileSet:
    profile_path = Path(path) if path is not None else default_profiles_path()
    try:
        with open(profile_path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot load ROM profiles from {profile_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ProfileError(f"{profile_path}: expected a mapping at the top level")
    return parse_profiles(document)


# -- identification --------------------------------------------------------------------


def _unsupported(
    reasons: list[str],
    *,
    sha1: str | None,
    sha256: str | None,
    game_code: str | None,
    version: int | None,
    size: int | None,
) -> RomIdentification:
    return RomIdentification(
        status=RomStatus.UNSUPPORTED_ROM,
        sha1=sha1,
        sha256=sha256,
        game_code=game_code,
        version=version,
        size=size,
        reasons=tuple(reasons),
    )


def identify_from_facts(
    *,
    sha1: str,
    sha256: str,
    header: bytes,
    size: int,
    profiles: ProfileSet | None = None,
) -> RomIdentification:
    """Core identification from measured facts (hashes, first 0xC0 header bytes, file size)."""
    profile_set = profiles if profiles is not None else load_profiles()
    if len(header) >= HEADER_SIZE:
        code_bytes = header[HEADER_GAME_CODE_OFFSET : HEADER_GAME_CODE_OFFSET + 4]
        game_code: str | None = code_bytes.decode("ascii", errors="replace")
        version: int | None = header[HEADER_VERSION_OFFSET]
    else:
        game_code, version = None, None
    facts = {
        "sha1": sha1,
        "sha256": sha256,
        "game_code": game_code,
        "version": version,
        "size": size,
    }

    candidates = profile_set.by_sha1(sha1)
    if not candidates:
        reasons = [f"sha1 {sha1} matches no known FireRed profile"]
        if game_code is None:
            reasons.append("file is smaller than the 0xC0-byte GBA header")
        else:
            reasons.append(
                f"header says {game_code!r} v{version}; not trusted without a hash match"
            )
        return _unsupported(reasons, **facts)

    # sha1 collisions between profiles are not expected; take the first and require the rest.
    profile = candidates[0]
    reasons = []
    if profile.rom.sha256 is not None and profile.rom.sha256 != sha256:
        reasons.append(
            f"sha1 matches {profile.profile_id} but sha256 {sha256} differs from the recorded one"
        )
    if game_code != profile.rom.game_code:
        reasons.append(
            f"header game code {game_code!r} != {profile.rom.game_code!r} of {profile.profile_id}"
        )
    if version != profile.rom.version:
        reasons.append(f"header version {version} != {profile.rom.version} of {profile.profile_id}")
    if profile.rom.size is not None and profile.rom.size != size:
        reasons.append(f"file size {size} != {profile.rom.size} of {profile.profile_id}")
    if reasons:
        return _unsupported(reasons, **facts)
    if not profile.supported:
        return _unsupported(
            [f"{profile.profile_id} is a known revision without a verified symbol table"],
            **facts,
        )
    return RomIdentification(
        status=RomStatus.SUPPORTED,
        profile_id=profile.profile_id,
        reasons=(),
        **facts,
    )


def identify_rom_image(data: bytes, profiles: ProfileSet | None = None) -> RomIdentification:
    """Identify an in-memory ROM image (used by tests; hashes the whole buffer)."""
    return identify_from_facts(
        sha1=hashlib.sha1(data).hexdigest(),
        sha256=hashlib.sha256(data).hexdigest(),
        header=bytes(data[:HEADER_SIZE]),
        size=len(data),
        profiles=profiles,
    )


def identify_rom_file(
    path: str | os.PathLike[str] | None = None,
    profiles: ProfileSet | None = None,
) -> RomIdentification:
    """Identify the ROM at ``path`` (default: ``$FIRERED_ROM_PATH``). Read-only, streaming.

    A missing path or file is ``AWAITING_ROM``. The extension is ignored.
    """
    raw_path = path if path is not None else os.environ.get(ROM_PATH_ENV)
    if not raw_path:
        return RomIdentification(
            status=RomStatus.AWAITING_ROM,
            reasons=(f"{ROM_PATH_ENV} is not set and no path was given",),
        )
    rom_path = Path(raw_path)
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    header = b""
    size = 0
    try:
        with open(rom_path, "rb") as handle:  # read-only; never written or copied
            while chunk := handle.read(_CHUNK):
                if size == 0:
                    header = chunk[:HEADER_SIZE]
                size += len(chunk)
                sha1.update(chunk)
                sha256.update(chunk)
    except FileNotFoundError:
        return RomIdentification(
            status=RomStatus.AWAITING_ROM, reasons=("ROM file does not exist",)
        )
    except OSError as exc:
        return RomIdentification(
            status=RomStatus.AWAITING_ROM, reasons=(f"ROM file is not readable: {exc.strerror}",)
        )
    return identify_from_facts(
        sha1=sha1.hexdigest(),
        sha256=sha256.hexdigest(),
        header=header,
        size=size,
        profiles=profiles,
    )


def require_supported(
    identification: RomIdentification, profiles: ProfileSet | None = None
) -> RomProfile:
    """Return the profile of a SUPPORTED identification, else raise :class:`UnsupportedRomError`."""
    if not identification.supported or identification.profile_id is None:
        raise UnsupportedRomError(identification)
    return (profiles if profiles is not None else load_profiles()).get(identification.profile_id)
