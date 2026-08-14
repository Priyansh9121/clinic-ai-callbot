# Slot booking: why there are no holds

`SlotStatus.HELD` and `AvailabilitySlot.held_until` exist in the schema and are
never written. This records why, so the next person to notice them does not
have to guess.

## What actually happens

```
search available slots        (no lock, no reservation)
        ↓
read three out to the caller  (optimistic — the rows can still change)
        ↓
caller chooses one
        ↓
caller confirms
        ↓
SELECT ... FOR UPDATE on that slot   ← the only lock, taken here
re-read status under the lock
INSERT appointment + mark slot BOOKED, in one transaction
        ↓
"You're booked for..."               ← said only after COMMIT
```

The window where two callers can hold the same offer is real: it runs from the
moment a slot is read out to the moment one of them says yes — seconds, but
unbounded if a caller goes quiet.

## Why that window is not closed with a hold

It is already closed correctly, just later and by the database rather than
earlier and by the application.

`SELECT ... FOR UPDATE` serialises the two confirmations. The second one blocks,
re-reads under the lock, sees `BOOKED`, and raises `SlotUnavailableError`. The
partial unique indexes on `appointments` are the backstop if a future code path
ever skips the lock. Exactly one appointment exists either way — pinned by
`test_two_callers_racing_for_one_slot_produce_exactly_one_booking` and
`test_many_callers_racing_for_one_slot`.

So a hold would not prevent a double booking. It would change *which* caller
loses and *when* they find out — and that is the whole of its benefit.

## What a hold would cost

Every one of these is required for a hold to be safe, and each is a place to
leak state:

- **Ownership.** A `HELD` row with no owner is worse than none: any caller could
  confirm into someone else's hold. It needs a session or call identifier.
- **Expiry.** A caller who hangs up mid-decision must not strand the slot, so
  every hold needs a TTL and something that honours it.
- **Release** on decline, on correction to another time, on hangup, on transfer,
  and on the conversation ending any other way.
- **A sweep** for the holds those paths miss — a crashed process leaves rows
  that nothing else will clear.
- **Clock discipline.** `held_until` is only meaningful against a single agreed
  clock; expiry compared in the wrong timezone silently never fires.

That is a scheduled job and five release paths, to change who hears "that time
has just been taken".

## What the losing caller hears

This is the part worth getting right, and it is where the effort went instead:

```
"Sorry, that time has just been taken.  I have Monday at 9 am, 12 pm or 3:30 pm.
 Would any of those work?"
```

`_execute_booking` catches `SlotUnavailableError`, clears the stale offers,
re-runs the search and prepends an apology — one turn, no transfer, no technical
error. Pinned by
`test_the_caller_who_loses_the_race_is_offered_alternatives`, which books the
contested slot out from under a caller sitting at the confirmation prompt.

## When to revisit

Implement holds if either becomes true:

- **Offers are made far in advance of confirmation** — an async or callback flow
  where minutes pass, not seconds. The window stops being negligible.
- **Losing a slot becomes expensive** — a scarce resource where being told "just
  taken" after a long qualification conversation is unacceptable.

Neither applies to a phone call where the caller answers within seconds. The
enum member and column are kept so that change needs no migration.
