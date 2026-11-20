"""Invented sign through actual field target planning and option construction."""
import importlib

from test_probe_primitives import snapshot

from pokegent.actions.field.builder import FieldBuilder
from pokegent.games.gen3.firered.maps.map_schema import (
    BgEventInfo,
    MapGrid,
    MapIdentity,
    MapLayoutInfo,
    MapState,
    PlayerPlacement,
)
from pokegent.goals.tracker import evaluate_probe
from pokegent.goals.validate import validate
from pokegent.options.models import DecisionFrame, assert_payload_clean, to_system_one_payload
from pokegent.options.registry import BuildContext, BuilderRegistry
from pokegent.pipeline import Perceiver, PerceptionKind
from pokegent.state.scenes import DetectorRegistry, SceneResult


def test_actual_reachable_sign_and_blocked_controls(monkeypatch):
    validator = importlib.import_module('pokegent.goals.validate')
    monkeypatch.setattr(validator, 'resolve_map', lambda _: (0, 0))
    monkeypatch.setattr(validator.map_names, 'map_name', lambda *_: 'DemoLab')
    verdict = validate(dict(objective_id='help', revision=1, kind='reach',
        target=dict(map='DemoLab'), rationale='Ask for help, then use the readable panel.',
        success_probes=[dict(kind='flag', flag=7)]))
    assert verdict.ok, verdict.reasons
    identity = MapIdentity(group=0, number=0, name='DemoLab', layout_id=1,
        region_section_id=0, map_type=0, map_type_name='indoor', weather=0,
        cave=0, floor_num=0, battle_type=0, music=0, biking_allowed=False,
        allow_escaping=False, allow_running=True, identity_verified=True)
    layout = MapLayoutInfo(width=3, height=3, border_width=1, border_height=1,
        border=(0,), primary_tileset=0, secondary_tileset=0, layout_ptr=0)
    player = PlayerPlacement(save_x=1, save_y=2, avatar_x=1, avatar_y=2,
        prev_x=1, prev_y=2, x=1, y=2, position_consistent=True, facing='N',
        elevation=0, object_event_id=0, avatar_flags=0, surfing=False,
        underwater=False, biking=False, controllable_flag=True, forced=False, moving=False,
        tile_transition_state=0, running_state=0)
    panel = BgEventInfo(index=0, x=1, y=1, elevation=0, kind_id=0, kind='sign',
                       sign_facing='N', hidden_item=None, script_ptr=0x08000100)
    blocks = [0x3ff] * (17 * 17)
    for y in range(3):
        for x in range(3):
            blocks[(y+7)*17+x+7] = 0
    blocks[(1+7)*17+1+7] = 0x400  # collidable panel, not a tile to stand on
    state = MapState(identity=identity, layout=layout,
        grid=MapGrid(columns=17, rows=17, blocks=tuple(blocks), attributes=((0, 0),)),
        player=player, bg_events=(panel,))
    builder = FieldBuilder(field_menu=False, objectives=lambda _: verdict.resolved)
    snap = snapshot().model_copy(update={'map':state})
    ctx = BuildContext(snapshot=snap, scene=SceneResult(scene_id='field.free',
        confidence=1, detector='invented'), goal_id='help')
    draft = builder.build(ctx)
    signs = [o for o in draft.options if o.chooseable and o.label == 'Read the sign']
    assert len(signs) == 1
    frame = DecisionFrame(run_id='invented', decision_id='decision-invented-0123456789',
        snapshot_seq=1,
        scene_id='field.free', scene_confidence=1, state_fingerprint=draft.state_fingerprint,
        options=draft.options, context=draft.context)
    assert_payload_clean(to_system_one_payload(frame), frame)
    assert not evaluate_probe(verdict.resolved.probes[0], snap)  # arrival is not proof
    assert evaluate_probe(verdict.resolved.probes[0], snapshot(flag=True))
    # No reachable approach: keep real Navigator/planner and make every tile blocked.
    blocked = state.model_copy(update={'grid':MapGrid(columns=17, rows=17,
        blocks=(0x400,)*(17*17), attributes=((0, 0),)),
        'player':player.model_copy(update={'x':0, 'save_x':0, 'avatar_x':0, 'prev_x':0})})
    other = builder.build(BuildContext(snapshot=snap.model_copy(update={'map':blocked}),
        scene=ctx.scene, goal_id='help'))
    assert not any(o.chooseable and o.label == 'Read the sign' for o in other.options)
    # Exercise the actual perception boundary, rather than inventing its classification.
    class Source:
        def read_snapshot(self):
            return snapshot(usable=False)
    registry = BuilderRegistry()
    registry.register_family('field', builder)
    invalid = Perceiver(Source(), DetectorRegistry(), registry).perceive('invalid')
    assert invalid.kind is PerceptionKind.INVALID
    assert invalid.scene.scene_id == 'system.invalid' and invalid.frame is None
