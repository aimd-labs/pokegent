"""Pinned map IDs are authoritative; all source inputs here are invented."""
import json

import pytest

from pokegent.games.gen3.firered.rom.setup_facts import Facts
from pokegent.games.gen3.firered.rom.setup_source import SetupError, Source


def fixture_source(ids=('MAP_XRAY_ROOM', 'MAP_PLAIN')):
    files = {'data/maps/map_groups.json': json.dumps(
        {'group_order': ['Demo'], 'Demo': ['XRayRoom', 'Plain']}).encode()}
    files.update({f'data/maps/{name}/map.json': json.dumps({'id': ident}).encode()
                  for name, ident in zip(('XRayRoom', 'Plain'), ids)})
    files.update({f'include/constants/{name}.h': b'' for name in
                  ('flags', 'opponents', 'menu', 'moves', 'metatile_behaviors')})
    return Source(files, 'invented')


def test_source_id_preserves_order_without_acronym_guess():
    facts = Facts(fixture_source())
    assert facts.map('MAP_XRAY_ROOM') == ('XRayRoom', [0, 0])
    assert facts.map('MAP_PLAIN') == ('Plain', [0, 1])
    assert facts.map_json('MAP_XRAY_ROOM')['id'] == 'MAP_XRAY_ROOM'
    with pytest.raises(SetupError, match='unresolved'):
        facts.map('MAP_X_RAY_ROOM')
    with pytest.raises(SetupError, match='unresolved'):
        facts.map('MAP_UNKNOWN')


@pytest.mark.parametrize('ids', [('MAP_PLAIN', 'MAP_PLAIN'),
                               ('bad', 'MAP_PLAIN'), (None, 'MAP_PLAIN')])
def test_invalid_or_duplicate_map_id_refused(ids):
    with pytest.raises(SetupError):
        Facts(fixture_source(ids))


def test_missing_or_changed_source_map_id_refused():
    source = fixture_source()
    del source.files['data/maps/Plain/map.json']
    with pytest.raises(SetupError):
        Facts(source)
    source = fixture_source()
    facts = Facts(source)
    source.files['data/maps/XRayRoom/map.json'] = b'{"id":"MAP_OTHER"}'
    with pytest.raises(SetupError, match='disagrees'):
        facts.map_json('MAP_XRAY_ROOM')
