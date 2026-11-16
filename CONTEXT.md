# Runtime invariants

Pokegent reads RAM and locally supplied ROM facts, offers typed actions, and verifies results.
One emulator has one writer. Unknown state or legality pauses execution. Revalidate options before
input and verify each action against current RAM. Never release a Pokémon, discard HMs or key
items, deposit the last healthy Pokémon, or confirm an irreversible prompt without explicit intent
in the current objective. Optional providers may fail open; action legality must fail closed.

Game inputs and locally generated data belong outside this checkout. See docs/local-data.md.
