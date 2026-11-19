"""Small pinned-file extractors; no source execution or general script interpreter.

Map-order and tileset layout recipes follow AIMD Labs worldgen/regiongen
(MIT fork retaining Nous Research attribution); pret is the format reference.
"""

from __future__ import annotations

import json
import re
import struct

from pokegent.games.gen3.firered.rom.rom_cache import _pret_macro_name
from pokegent.games.gen3.firered.rom.setup_source import SetupError, Source


def one(pattern: str, text: str) -> tuple[str, ...]:
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise SetupError('required source fact missing or ambiguous')
    return matches[0].groups()


def script_block(text: str, label: str) -> str:
    # Multiline block capture, bounded by a sibling label; never follow calls.
    matches = list(re.finditer(r'^' + re.escape(label) + r'::?[^\n]*\n'
                              r'([\s\S]*?)(?=^\w+::?|\Z)', text, re.M))
    if len(matches) != 1:
        raise SetupError('required script label missing or ambiguous')
    return matches[0][1]


def message(source: Source, file: str, label: str) -> str:
    body = script_block(source.text(file), label)
    parts = re.findall(r'^\s*\.string "((?:[^"\\]|\\.)*)"\s*$', body, re.M)
    if not parts or any(line.strip() and not line.lstrip().startswith(('.string', '@'))
                        for line in body.splitlines()):
        raise SetupError('unsupported message definition')
    try:
        value = ''.join(json.loads('"' + re.sub(r'\\[pl]', r'\\n', part) + '"')
                        for part in parts)
    except ValueError:
        raise SetupError('unsupported message escape') from None
    if not value.endswith('$') or '$' in value[:-1]:
        raise SetupError('message terminator missing or ambiguous')
    return ' '.join(value[:-1].split())


class Facts:
    def __init__(self, source: Source):
        self.source = source
        raw = source.json('data/maps/map_groups.json')
        self.groups = {'group_order': raw['group_order'],
                       'groups': {key: raw[key] for key in raw['group_order']}}
        self.maps = {_pret_macro_name(name): (name, [g, n])
                     for g, key in enumerate(raw['group_order'])
                     for n, name in enumerate(raw[key])}
        self.defines = {}
        for file in ('flags', 'opponents', 'menu', 'moves', 'metatile_behaviors'):
            for name, value in re.findall(r'^#define\s+(\w+)\s+([^\n]+)',
                                          source.text(f'include/constants/{file}.h'), re.M):
                value = value.split('//')[0].split('/*')[0].strip()
                if name in self.defines and self.defines[name] != value:
                    raise SetupError('ambiguous source constant')
                self.defines[name] = value

    def number(self, name: str, seen: tuple[str, ...] = ()) -> int:
        """Only pinned headers' literal/alias and flat additive flag recipes."""
        if re.fullmatch(r'0x[\da-fA-F]+|\d+', name):
            return int(name, 0) if name.startswith('0x') else int(name)
        if name in seen or len(seen) >= 12 or name not in self.defines:
            raise SetupError('unresolved or cyclic source constant')
        expr = self.defines[name]
        if expr.startswith('(') and expr.endswith(')'):
            expr = expr[1:-1].strip()
        if not re.fullmatch(r'\w+(?:\s*[+-]\s*\w+){0,3}', expr):
            raise SetupError('unsupported source constant expression')
        tokens = re.findall(r'\w+|[+-]', expr)
        value = self.number(tokens[0], seen + (name,))
        for op, term in zip(tokens[1::2], tokens[2::2]):
            value += (1 if op == '+' else -1) * self.number(term, seen + (name,))
        if not 0 <= value <= 0xFFFF:
            raise SetupError('source constant outside supported range')
        return value

    def map(self, constant: str) -> tuple[str, list[int]]:
        try:
            return self.maps[constant]
        except KeyError:
            raise SetupError('source map constant unresolved') from None

    def map_json(self, constant: str) -> dict:
        name, _ = self.map(constant)
        document = self.source.json(f'data/maps/{name}/map.json')
        if document['id'] != constant:
            raise SetupError('source map id disagrees with ordered index')
        return document


class Tiles:
    """Pinned mapjson/INCBIN layout; only static tile facts, never source code."""

    def __init__(self, source: Source, map_json: dict):
        layouts = source.json('data/layouts/layouts.json')['layouts']
        matches = [x for x in layouts if x.get('id') == map_json['layout']]
        if len(matches) != 1:
            raise SetupError('source layout missing or ambiguous')
        layout = matches[0]
        self.width, self.height = layout['width'], layout['height']
        if not (1 <= self.width <= 512 and 1 <= self.height <= 512):
            raise SetupError('unsupported map dimensions')
        self.source = source
        self.blocks = self._bytes(layout['blockdata_filepath'], 2, self.width * self.height)
        self.attrs = []
        headers = source.text('src/data/tilesets/headers.h')
        metatiles = source.text('src/data/tilesets/metatiles.h')
        for key in ('primary_tileset', 'secondary_tileset'):
            body = one(r'const struct Tileset ' + re.escape(layout[key])
                       + r'\s*=\s*\{([\s\S]*?)\n\};', headers)[0]
            symbol = one(r'\.metatileAttributes\s*=\s*(\w+)', body)[0]
            path = one(re.escape(symbol) + r'\[\]\s*=\s*INCBIN_U32\("([^"]+)"\)',
                       metatiles)[0]
            self.attrs.append(self._bytes(path, 4))

    def _bytes(self, path, width, count=None):
        try:
            raw = self.source.files[path]
        except KeyError:
            raise SetupError('source tile bytes unavailable') from None
        if len(raw) % width or count is not None and len(raw) != width * count:
            raise SetupError('source tile dimensions disagree')
        return struct.unpack('<' + ('H' if width == 2 else 'I') * (len(raw) // width), raw)

    def tile(self, x: int, y: int) -> tuple[int, int, int]:
        if not 0 <= x < self.width or not 0 <= y < self.height:
            raise SetupError('source coordinate outside map')
        value = self.blocks[y * self.width + x]
        index = value & 0x3FF
        attrs = self.attrs[0 if index < 640 else 1]
        index = index if index < 640 else index - 640
        if index >= len(attrs):
            raise SetupError('source metatile attribute unavailable')
        return (value >> 10) & 3, value >> 12, attrs[index] & 0x1FF
