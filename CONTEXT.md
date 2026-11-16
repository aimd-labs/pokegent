# Runtime invariants

Pokegent reads RAM and locally supplied ROM facts, offers typed actions, and verifies results.
One emulator has one writer. Unknown legality pauses execution. Unknown scenes also pause, except
the hybrid window of conservative agent primitives (on by default, opt-out; docs/contracts.md,
rule 8). Revalidate before input and verify each action against current RAM. Never release a
Pokémon, discard HMs or key items, deposit the last healthy Pokémon, or confirm an irreversible
prompt without explicit intent
in the current objective. Optional providers may fail open; action legality must fail closed.

Game inputs and locally generated data belong outside this checkout. See docs/local-data.md.
