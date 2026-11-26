# Game plugin acceptance checklist

This page maps the game-plugin acceptance from [issue #139](https://github.com/aimd-labs/pokegent/issues/139),
"a second game works without new core gameplay branches", onto the seams that exist today:
opt-in exact-ROM plugin selection, FireRed composition, the generic observation envelope and
bounded plugin metadata. Every proof named below is a test that ships in the repository and
runs on synthetic input only. There is no second-title plugin, no native runtime input and no
ROM involved; no second title or support status is promised, and exact-ROM identification must
stay fail-closed.

| Requirement | Existing proof |
|---|---|
| Plugin registry is opt-in: disabled by default and refuses before compose | tests/test_game_plugin_selection.py::test_registry_defaults_off_and_refuses_before_compose |
| The enable flag must be exactly a bool | tests/test_game_plugin_selection.py::test_registry_enabled_flag_must_be_exactly_bool |
| Exact-ROM selection is fail-closed: unknown digest, owner mismatch, unsupported contract version and missing required capability all refuse | tests/test_game_plugin_selection.py::test_unknown_digest_fails_closed_without_compose, test_exact_requested_owner_mismatch_fails_closed, test_unsupported_contract_version_fails_closed, test_missing_required_capability_fails_closed |
| Inputs are bounded before compose; caller buffers are hashed without mutation, and memoryview bounds count bytes | tests/test_game_plugin_selection.py::test_bounded_inputs_are_rejected_before_compose, test_memoryview_bound_counts_bytes_not_elements |
| Duplicate plugin identity or profile ownership is rejected | tests/test_game_plugin_selection.py::test_duplicate_identity_or_profile_ownership_is_rejected |
| Descriptor metadata is typed and bounded: identifiers, capability set, digest, plain-int contract version, immutability | tests/test_game_plugin_metadata.py::test_descriptor_rejects_non_exact_blank_or_overlong_identifiers, test_bool_contract_version_is_still_rejected, test_descriptor_is_immutable |
| Admission selects by exact ROM digest and bounds required capabilities; bad identifiers refuse before hashing and compose | tests/test_game_plugin_metadata.py::test_select_rejects_bad_identifiers_before_hashing_and_compose, test_select_bounds_required_capability_consumption_and_skips_compose, test_select_still_accepts_exact_rom_finite_capability_generator |
| Composition registers game-owned factories, composes once after admission, and a refusal never calls a factory | tests/test_firered_plugin_composition.py::test_compose_stores_the_game_owned_factories_without_calling_them, test_admitted_build_uses_the_composed_factories_once, test_refused_admission_never_calls_a_factory |
| Composed capabilities cover exactly the supplied source factories | tests/test_firered_plugin_composition.py::test_capabilities_cover_exactly_the_supplied_source_factories |
| The default non-plugin path keeps the app helpers and never composes | tests/test_firered_plugin_composition.py::test_default_flag_keeps_the_app_helpers_and_never_composes |
| Observation envelope is opt-in and validates every record: schema version, identities, counters, usable, immutable caller-data copy | tests/test_game_observation_envelope.py::test_factory_is_off_by_default_and_refuses_without_opt_in, test_schema_version_is_exact, test_identities_are_nonempty_bounded_strings, test_record_and_payload_are_immutable_and_copy_caller_data |
| Observation sequences stay strictly increasing per epoch; rollback is rejected, an epoch increase allows reset, and unusable never becomes ready | tests/test_game_observation_envelope.py::test_same_epoch_sequence_must_strictly_increase, test_cross_identity_and_epoch_rollback_are_rejected, test_unusable_stays_unknown_and_never_becomes_ready |
| Two synthetic games share the same valid observation API | tests/test_game_observation_envelope.py::test_two_synthetic_games_share_the_same_valid_api |
| A second game title composes and runs through the pipeline without new core branches | partially covered: a synthetic second title is admitted by exact digest, composes once and produces a valid observation sequence in tests/test_second_title_plugin.py::test_second_title_admission_composes_services (unknown digests are refused by test_unknown_digest_is_refused_and_never_composes); running the title through the full pipeline is not covered |
| Gameplay, native runtime or campaign parity for a second title | not covered |

Rows marked `not covered` have no repository test today; filling them is exactly the remaining
gap for issue #139. The covered rows are bounded synthetic seams, not native or release proof,
and nothing here promotes a support status for any second title.
