#!/usr/bin/env python3
"""
IBM 026 Keypunch — relay logic simulation.

Implements the column-cycle control network as discrete Python objects
that mirror the electromechanical components in the real machine:

  Relay            — single relay coil with NO/NC contacts
  LatchRelay       — self-holding relay (SET/RESET coils)
  DrumSensor       — 12 sensing brushes reading the program drum
  ShiftGateNetwork — zone-row blocking relays (numeric vs alpha mode)
  PunchSolenoids   — 12 solenoid drivers, one per row
  IBM026           — wires everything together; runs column cycles

Signal flow per column cycle
-----------------------------
  1. DrumSensor reads 12 brush positions for current column
  2. FIELD_START relay fires if row-12 brush is closed
  3. FIELD_START resets all mode latches, then:
       12 alone  →  ALPHA_LATCH set
       12 + 11   →  NUMERIC_LATCH set
       12 + 0    →  SKIP_LATCH set
  4a. SKIP:    keyboard interlock energized; carriage advances; no punch
  4b. DUP key: read-station brushes drive row bus directly → solenoids
  4c. Normal:  key press → row bus → ShiftGateNetwork → solenoids → punch
  5.  Column counter advances; card ejects at column 80

Usage
-----
  python ibm026.py                     # interactive REPL, payroll drum
  python ibm026.py --drum inventory    # inventory drum
  python ibm026.py --drum blank        # all-alpha drum
  python ibm026.py --demo              # automated demo, then exit
  python ibm026.py --verbose           # trace relay states each cycle
"""

from __future__ import annotations
import sys
import argparse
from typing import Optional

# ---------------------------------------------------------------------------
# Hollerith row names and character encoding
# ---------------------------------------------------------------------------

# The 12 punching positions of a Hollerith card, top to bottom
ROWS: tuple[str, ...] = ('12', '11', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9')

NUM_COLS = 80

# IBM 026 commercial character set: character → frozenset of row names punched
HOLLERITH: dict[str, frozenset[str]] = {
    # Digits
    '0': frozenset({'0'}),
    '1': frozenset({'1'}),  '2': frozenset({'2'}),  '3': frozenset({'3'}),
    '4': frozenset({'4'}),  '5': frozenset({'5'}),  '6': frozenset({'6'}),
    '7': frozenset({'7'}),  '8': frozenset({'8'}),  '9': frozenset({'9'}),
    # Letters A–I  (zone 12 + digit)
    'A': frozenset({'12','1'}), 'B': frozenset({'12','2'}), 'C': frozenset({'12','3'}),
    'D': frozenset({'12','4'}), 'E': frozenset({'12','5'}), 'F': frozenset({'12','6'}),
    'G': frozenset({'12','7'}), 'H': frozenset({'12','8'}), 'I': frozenset({'12','9'}),
    # Letters J–R  (zone 11 + digit)
    'J': frozenset({'11','1'}), 'K': frozenset({'11','2'}), 'L': frozenset({'11','3'}),
    'M': frozenset({'11','4'}), 'N': frozenset({'11','5'}), 'O': frozenset({'11','6'}),
    'P': frozenset({'11','7'}), 'Q': frozenset({'11','8'}), 'R': frozenset({'11','9'}),
    # Letters S–Z  (zone 0 + digit)
    'S': frozenset({'0','2'}),  'T': frozenset({'0','3'}),  'U': frozenset({'0','4'}),
    'V': frozenset({'0','5'}),  'W': frozenset({'0','6'}),  'X': frozenset({'0','7'}),
    'Y': frozenset({'0','8'}),  'Z': frozenset({'0','9'}),
    # Space — no punches
    ' ': frozenset(),
}

# Reverse map for decoding a column back to a character
HOLLERITH_DECODE: dict[frozenset[str], str] = {v: k for k, v in HOLLERITH.items()}

# Characters valid in numeric mode (no zone-punch required)
NUMERIC_CHARS: frozenset[str] = frozenset('0123456789 ')


# ---------------------------------------------------------------------------
# Relay primitives
# ---------------------------------------------------------------------------

class Relay:
    """
    Single electromagnetic relay.

    The coil, when energized, closes NO (normally open) contacts and
    opens NC (normally closed) contacts.  In relay logic schematics these
    contacts appear as rungs in ladder diagrams.
    """

    def __init__(self, name: str = ''):
        self.name = name
        self._energized: bool = False

    # --- Contacts ---

    @property
    def NO(self) -> bool:
        """Normally Open contact: passes (True) when relay is energized."""
        return self._energized

    @property
    def NC(self) -> bool:
        """Normally Closed contact: passes (True) when relay is released."""
        return not self._energized

    # --- Coil ---

    def drive(self, signal: bool) -> None:
        """Apply a signal to the coil.  True energizes; False releases."""
        self._energized = bool(signal)

    # --- Convenience ---

    def __bool__(self) -> bool:
        return self._energized

    def __repr__(self) -> str:
        s = 'ENERGIZED' if self._energized else 'released'
        return f'Relay({self.name!r}, {s})'


class LatchRelay(Relay):
    """
    Self-holding (latching) relay.

    Ladder-logic equivalent
    -----------------------
        SET coil  ──┤SET_in├──────────────────(coil)
        hold      ──┤self.NO├──┤RESET_in NC├──(coil)

    Once SET fires, the relay holds itself closed through its own NO contact.
    RESET breaks the hold circuit, releasing the relay.
    If both SET and RESET are simultaneously asserted, RESET wins.
    """

    def set(self, signal: bool) -> None:
        """Energize SET coil.  Latches ON if signal is True."""
        if signal:
            self._energized = True

    def reset(self, signal: bool) -> None:
        """Energize RESET coil.  Releases latch if signal is True."""
        if signal:
            self._energized = False

    def __repr__(self) -> str:
        s = 'LATCHED' if self._energized else 'released'
        return f'LatchRelay({self.name!r}, {s})'


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

class Card:
    """
    80-column, 12-row punched card.

    Holes are stored as a dict of column (0-indexed) → set of row names.
    """

    def __init__(self) -> None:
        self._holes: dict[int, set[str]] = {c: set() for c in range(NUM_COLS)}

    def punch(self, col: int, rows: frozenset[str]) -> None:
        """Punch holes at column col for the given row names."""
        self._holes[col].update(rows)

    def sense(self, col: int) -> frozenset[str]:
        """Return the set of rows punched at column col."""
        return frozenset(self._holes[col])

    def decode_col(self, col: int) -> str:
        """Decode a column to its Hollerith character, or '?' if unknown."""
        rows = self.sense(col)
        return HOLLERITH_DECODE.get(rows, '?' if rows else ' ')

    def to_text(self) -> str:
        """Decode all 80 columns to a text string."""
        return ''.join(self.decode_col(c) for c in range(NUM_COLS))

    def display(self, title: str = 'Card') -> str:
        """
        Return a visual punch-card layout.

        Each row is a horizontal line; holes are marked with 'O'.
        Column numbers run across the top.
        """
        lines: list[str] = []
        lines.append(f'  {title}')
        # Column-number header (tens digit then units)
        tens = '     |' + ''.join(
            str((c + 1) // 10) if (c + 1) % 10 == 0 else ' ' for c in range(NUM_COLS)
        ) + '|'
        units = '     |' + ''.join(str((c + 1) % 10) for c in range(NUM_COLS)) + '|'
        lines.append(tens)
        lines.append(units)
        lines.append('     +' + '-' * NUM_COLS + '+')
        for row in ROWS:
            marks = ''.join('O' if row in self._holes[c] else '.' for c in range(NUM_COLS))
            lines.append(f'  {row:>2} |{marks}|')
        lines.append('     +' + '-' * NUM_COLS + '+')
        lines.append('  txt|' + self.to_text() + '|')
        return '\n'.join(lines)

    @classmethod
    def blank(cls) -> 'Card':
        return cls()


# ---------------------------------------------------------------------------
# Drum sensor
# ---------------------------------------------------------------------------

class DrumSensor:
    """
    12 sensing brushes riding on the program drum.

    The drum rotates in lock-step with the card transport: each time the
    data card advances one column, the drum presents the corresponding
    program-card column to the brushes.  A hole in the program card closes
    the brush circuit (True); card stock keeps it open (False).
    """

    def __init__(self, drum_card: Card) -> None:
        self.drum_card = drum_card

    def sense(self, col: int) -> dict[str, bool]:
        """
        Read all 12 brush positions for column col.
        Returns {row_name: hole_present} for every row.
        """
        punched = self.drum_card.sense(col)
        return {row: (row in punched) for row in ROWS}


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

class Keyboard:
    """
    Models the keypunch keyboard as a switch matrix.

    Each key closes contacts that connect specific row lines to the row bus.
    The keyboard interlock relay (energized in skip mode) physically
    disconnects the keyboard from the bus.

    In numeric mode the keyboard is constrained: only digit characters are
    accepted.  (On the real machine a mechanical interlock blocked the
    alpha keytops in numeric shift.)
    """

    def __init__(self) -> None:
        # Interlock relay: NO contact energized → keyboard locked out
        self.interlock: Relay = Relay('KB_INTERLOCK')

    def press(self, char: str, numeric_mode: bool = False) -> frozenset[str]:
        """
        Simulate a key press.

        Returns the rows driven onto the row bus, or raises:
          RuntimeError  — keyboard is locked (interlock energized)
          ValueError    — character not allowed in current mode
          KeyError      — character has no Hollerith encoding
        """
        if self.interlock.NO:
            raise RuntimeError('Keyboard locked out (skip mode)')

        char = char.upper()
        if char not in HOLLERITH:
            raise KeyError(f'No Hollerith encoding for {char!r}')

        if numeric_mode and char not in NUMERIC_CHARS:
            raise ValueError(
                f'Key {char!r} not valid in NUMERIC mode '
                f'(keyboard interlock prevents alpha keypresses)'
            )

        return HOLLERITH[char]

    def lock(self) -> None:
        self.interlock.drive(True)

    def unlock(self) -> None:
        self.interlock.drive(False)


# ---------------------------------------------------------------------------
# Shift gate network
# ---------------------------------------------------------------------------

class ShiftGateNetwork:
    """
    Zone-row blocking relay network between the row bus and punch solenoids.

    In NUMERIC mode the NUMERIC_LATCH energizes two blocking relays:
      ZONE12_GATE and ZONE11_GATE.

    Each blocking relay's NC contact is in series with the corresponding
    solenoid driver.  When the relay is energized its NC contact opens,
    breaking the solenoid circuit for that row.

    Row '0' is intentionally NOT blocked in numeric mode: it is both the
    digit-zero punch (0-row alone) and part of the zone used for letters
    S–Z.  The keyboard interlock already prevents S–Z from being typed in
    numeric mode, so row-0 solenoid signals in numeric mode are always
    legitimately digit zero.

    In ALPHA mode both blocking relays are released → all NC contacts
    closed → full row set (including zones 12 and 11) reaches solenoids.
    """

    def __init__(self) -> None:
        self.zone12_gate: Relay = Relay('ZONE12_GATE')
        self.zone11_gate: Relay = Relay('ZONE11_GATE')

    def set_numeric(self) -> None:
        """Energize blocking relays → NC contacts open → zones 12,11 blocked."""
        self.zone12_gate.drive(True)
        self.zone11_gate.drive(True)

    def set_alpha(self) -> None:
        """Release blocking relays → NC contacts closed → all rows pass."""
        self.zone12_gate.drive(False)
        self.zone11_gate.drive(False)

    def gate(self, rows: frozenset[str]) -> frozenset[str]:
        """
        Pass a row set through the gate network.

        Zone rows whose blocking relay is energized (NC contact open)
        are removed from the set before it reaches the solenoids.
        """
        passed: set[str] = set()
        for row in rows:
            if row == '12' and self.zone12_gate.NO:
                continue   # NC contact open — row 12 blocked
            if row == '11' and self.zone11_gate.NO:
                continue   # NC contact open — row 11 blocked
            passed.add(row)
        return frozenset(passed)

    def state(self) -> str:
        if self.zone12_gate.NO:
            return 'NUMERIC (zones 12,11 blocked)'
        return 'ALPHA (all rows pass)'


# ---------------------------------------------------------------------------
# Punch solenoids
# ---------------------------------------------------------------------------

class PunchSolenoids:
    """
    12 punch solenoids, one per row.

    Each solenoid, when energized within the cam timing window, drives a
    punch die through the card stock, cutting a rectangular hole.

    In the real machine the timing window is enforced by a cam-driven
    normally-open contact in series with each solenoid.  Here we model the
    solenoid as simply recording which rows were fired.
    """

    def fire(self, rows: frozenset[str]) -> frozenset[str]:
        """Fire solenoids for the given rows.  Returns rows punched."""
        return frozenset(rows)


# ---------------------------------------------------------------------------
# IBM 026 Keypunch
# ---------------------------------------------------------------------------

class IBM026:
    """
    IBM 026 Keypunch — complete relay logic simulation.

    Instantiate with a drum Card, then call step() for each column.
    """

    def __init__(self, drum_card: Card, verbose: bool = False) -> None:
        self.verbose = verbose

        # --- Sub-assemblies ---
        self.drum      = DrumSensor(drum_card)
        self.keyboard  = Keyboard()
        self.gates     = ShiftGateNetwork()
        self.solenoids = PunchSolenoids()

        # --- Relays ---

        # Momentary relay: energizes during the sensing window when row 12
        # is present on the drum.  De-energizes at the end of the window.
        self.field_start   = Relay('FIELD_START')

        # Self-holding mode latches — one stays set until the next
        # FIELD_START pulse resets all three and sets the new one.
        self.alpha_latch   = LatchRelay('ALPHA_LATCH')
        self.numeric_latch = LatchRelay('NUMERIC_LATCH')
        self.skip_latch    = LatchRelay('SKIP_LATCH')

        # --- Card registers ---
        self.column: int = 0
        self.data_card: Card = Card()          # card being punched
        self.read_card: Optional[Card] = None  # card in the read station

        # Initialize mode for column 0 (machine powers up in alpha shift
        # if no program punch is present at column 0)
        self.alpha_latch.set(True)             # default: alpha mode
        self._sense_and_latch()                # may override with drum data
        self._configure_gates()

    # ------------------------------------------------------------------
    # Column cycle internals
    # ------------------------------------------------------------------

    def _sense_and_latch(self) -> dict[str, bool]:
        """
        Stages 1–3 of the column cycle.

        1. DrumSensor reads all 12 brush positions for self.column.
        2. FIELD_START relay is driven by the row-12 brush.
        3. If FIELD_START fires:
             a. RESET coils of all three latches are driven (hold circuits
                broken — all latches release).
             b. The appropriate new latch is set by the combinational gate
                formed from the remaining sensed rows.
        """
        sense = self.drum.sense(self.column)

        # Stage 2: FIELD_START coil driven by row-12 brush
        self.field_start.drive(sense['12'])

        if self.field_start.NO:
            # Stage 3a: Reset all latches.
            # In ladder logic: FIELD_START.NO in series with each RESET coil.
            self.alpha_latch.reset(True)
            self.numeric_latch.reset(True)
            self.skip_latch.reset(True)

            # Stage 3b: Combinational gate to select the new mode.
            # Priority: skip (12+0) > numeric (12+11) > alpha (12 alone)
            is_skip    = sense['0']
            is_numeric = sense['11'] and not sense['0']
            is_alpha   = not sense['11'] and not sense['0']

            self.skip_latch.set(is_skip)
            self.numeric_latch.set(is_numeric)
            self.alpha_latch.set(is_alpha)

        if self.verbose:
            self._trace_sense(sense)

        return sense

    def _configure_gates(self) -> None:
        """
        Drive the ShiftGateNetwork from the current latch state.

        NUMERIC_LATCH.NO energizes the zone-blocking relays.
        Any other mode (alpha, skip) releases them.
        """
        if self.numeric_latch.NO:
            self.gates.set_numeric()
        else:
            self.gates.set_alpha()

    def _advance(self) -> bool:
        """
        Advance the carriage one column.  Returns True if the card ejected.
        """
        self.column += 1
        if self.column >= NUM_COLS:
            self._eject()
            return True
        return False

    def _eject(self) -> None:
        """
        Eject the current data card to the read station; load a blank card.
        Re-sense column 0 to establish mode for the new card.
        """
        self.read_card = self.data_card
        self.data_card = Card()
        self.column = 0
        self.alpha_latch.set(True)   # default mode for new card
        self._sense_and_latch()
        self._configure_gates()

    # ------------------------------------------------------------------
    # Verbose tracing
    # ------------------------------------------------------------------

    def _trace_sense(self, sense: dict[str, bool]) -> None:
        rows = [r for r in ROWS if sense[r]]
        print(f'  [drum  col {self.column+1:02d}] rows: {rows or ["none"]:}')
        print(f'  [latch       ] FIELD_START={self.field_start.NO} '
              f'ALPHA={self.alpha_latch.NO} '
              f'NUMERIC={self.numeric_latch.NO} '
              f'SKIP={self.skip_latch.NO}')
        print(f'  [gates       ] {self.gates.state()}')

    def _trace_punch(self, col: int, char: str, bus: frozenset, gated: frozenset,
                     punched: frozenset) -> None:
        print(f'  [key   col {col+1:02d}] char={char!r} '
              f'bus={sorted(bus)} → gated={sorted(gated)} → punched={sorted(punched)}')

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """Current field mode as a string."""
        if self.skip_latch.NO:    return 'SKIP'
        if self.numeric_latch.NO: return 'NUMERIC'
        return 'ALPHA'

    def step(self, key: Optional[str] = None, dup: bool = False) -> dict:
        """
        Execute one column cycle.

        Parameters
        ----------
        key : str or None
            Character key pressed by the operator.  None means no key
            pressed yet; the method returns action='waiting' in that case.
        dup : bool
            True if the operator is holding the DUP key.  The read-station
            brushes substitute for the keyboard on the row bus.

        Returns
        -------
        dict with keys:
            column  : int   (1-indexed)
            mode    : str
            action  : str   ('punch', 'skip', 'dup', 'dup_no_card', 'waiting')
            bus     : frozenset   rows on the row bus before gating
            gated   : frozenset   rows after shift gates
            punched : frozenset   rows actually punched
            char    : str         character representation of the punch
            ejected : bool        True if the card was ejected this cycle
        """
        col  = self.column
        mode = self.mode
        result = dict(column=col + 1, mode=mode, action=None,
                      bus=frozenset(), gated=frozenset(),
                      punched=frozenset(), char=' ', ejected=False)

        # ── SKIP ──────────────────────────────────────────────────────
        if self.skip_latch.NO:
            # Skip latch energizes keyboard interlock → keyboard disconnected.
            # Carriage transport motor advances the card directly.
            self.keyboard.lock()
            result['action'] = 'skip'
            ejected = self._advance()
            if not ejected:
                self._sense_and_latch()
                self._configure_gates()
            self.keyboard.unlock()
            result['ejected'] = ejected
            return result

        # ── DUP key held ──────────────────────────────────────────────
        if dup:
            if self.read_card is None:
                result['action'] = 'dup_no_card'
            else:
                # Read-station brushes substituted for keyboard on row bus.
                # Bypasses shift gate network — copies whatever is on the
                # previous card, zone rows and all.
                bus = self.read_card.sense(col)
                punched = self.solenoids.fire(bus)
                self.data_card.punch(col, punched)
                result.update(action='dup', bus=bus, gated=bus,
                              punched=punched,
                              char=self.data_card.decode_col(col))
                if self.verbose:
                    print(f'  [dup   col {col+1:02d}] copied rows {sorted(bus)}')
            ejected = self._advance()
            if not ejected:
                self._sense_and_latch()
                self._configure_gates()
            result['ejected'] = ejected
            return result

        # ── Waiting for key ───────────────────────────────────────────
        if key is None:
            result['action'] = 'waiting'
            return result

        # ── Normal key press ──────────────────────────────────────────
        # Stage 4c — keyboard → row bus → shift gates → solenoids → punch

        self.keyboard.unlock()

        # Keyboard switch matrix drives row bus
        bus = self.keyboard.press(key, numeric_mode=self.numeric_latch.NO)

        # ShiftGateNetwork: NC contacts of zone-blocking relays
        # filter zone rows in numeric mode
        gated = self.gates.gate(bus)

        # Solenoid drivers fire within the cam timing window
        punched = self.solenoids.fire(gated)
        self.data_card.punch(col, punched)

        char = self.data_card.decode_col(col)

        result.update(action='punch', bus=bus, gated=gated,
                      punched=punched, char=char)

        if self.verbose:
            self._trace_punch(col, key, bus, gated, punched)

        # Advance carriage
        ejected = self._advance()
        if not ejected:
            self._sense_and_latch()
            self._configure_gates()
        result['ejected'] = ejected

        return result

    def run_field(self, text: str, dup: bool = False) -> list[dict]:
        """
        Punch an entire string of characters, one per column.
        Auto-skips are handled automatically between fields.
        Returns list of step result dicts.
        """
        results = []
        for ch in text:
            # Flush any pending skip columns first
            while self.skip_latch.NO and self.column < NUM_COLS:
                results.append(self.step())
            if self.column >= NUM_COLS:
                break
            results.append(self.step(key=ch, dup=dup))
        return results

    def flush_skips(self) -> int:
        """
        Advance through all consecutive skip columns.
        Returns the column number (1-indexed) where the machine stopped.
        """
        count = 0
        while self.skip_latch.NO and self.column < NUM_COLS:
            self.step()
            count += 1
        return self.column + 1

    def press_skip_key(self) -> int:
        """
        Operator presses the manual SKIP key.

        Relay logic
        -----------
        The SKIP key is a normally-open pushbutton whose contact, when
        closed, completes the circuit to the SET coil of SKIP_LATCH —
        exactly the same coil that the drum-sensor path energizes when it
        sees a 12+0 punch.  The drum is bypassed entirely; the latch
        doesn't care which path set it.

        Once SKIP_LATCH latches on:
          - KB_INTERLOCK energizes (NC contact opens, keyboard disconnected)
          - The transport motor advances the carriage column by column
          - Each column cycle calls _sense_and_latch(); when the drum
            presents the next field boundary (row 12), FIELD_START fires,
            SKIP_LATCH resets, and the new mode latch sets

        The current column is left unpunched — the operator has abandoned
        the remainder of the field.

        Returns the column number (1-indexed) where the machine stopped.
        """
        if self.verbose:
            print(f'  [SKIP key  col {self.column+1:02d}] '
                  f'SET coil energized → SKIP_LATCH latches')

        # SET coil of SKIP_LATCH driven by the key contact
        self.skip_latch.set(True)
        self._configure_gates()

        # Run forward until the next field boundary clears the latch
        stopped_at = self.flush_skips()

        if self.verbose:
            print(f'  [SKIP key] field boundary → col {stopped_at}, '
                  f'mode now {self.mode}')

        return stopped_at

    def status(self) -> str:
        return (
            f'Col {self.column+1:02d}/{NUM_COLS} | '
            f'Mode: {self.mode:<8} | '
            f'Gates: {self.gates.state()}'
        )

    def relay_states(self) -> str:
        relays = [
            self.field_start,
            self.alpha_latch,
            self.numeric_latch,
            self.skip_latch,
            self.gates.zone12_gate,
            self.gates.zone11_gate,
            self.keyboard.interlock,
        ]
        return '\n'.join(f'  {r}' for r in relays)


# ---------------------------------------------------------------------------
# Drum card builder
# ---------------------------------------------------------------------------

def make_drum_card(
    fields: list[tuple[int, int, str, str]]
) -> Card:
    """
    Build a program drum card from a field specification.

    Each field tuple: (start_col, end_col, type, description)
      start_col, end_col — 1-indexed, inclusive
      type — 'alpha', 'numeric', or 'skip'

    Only the first column of each field receives the control punch.
    Subsequent columns are blank (the mode latch holds the state).
    """
    card = Card()
    for start, end, ftype, *_ in fields:
        col = start - 1   # convert to 0-indexed
        if ftype == 'alpha':
            card.punch(col, frozenset({'12'}))           # row 12 alone
        elif ftype == 'numeric':
            card.punch(col, frozenset({'12', '11'}))     # row 12 + row 11
        elif ftype == 'skip':
            card.punch(col, frozenset({'12', '0'}))      # row 12 + row 0
        # 'dup' fields are numeric on the drum; duplication is operator-initiated
    return card


# ---------------------------------------------------------------------------
# Pre-built drum card definitions
# ---------------------------------------------------------------------------

DRUMS: dict[str, dict] = {
    'payroll': {
        'description': 'Weekly hourly payroll record',
        'fields': [
            (1,  9,  'numeric', 'Employee number'),
            (10, 29, 'alpha',   'Employee name'),
            (30, 35, 'numeric', 'Pay period date (MMDDYY)'),
            (36, 42, 'numeric', 'Regular hours   (HHHtttt)'),
            (43, 49, 'numeric', 'Overtime hours  (HHHtttt)'),
            (50, 57, 'numeric', 'Pay rate        (DDDDcccc)'),
            (58, 80, 'skip',    '(unused)'),
        ],
    },
    'inventory': {
        'description': 'Warehouse inventory stock record',
        'fields': [
            (1,  8,  'numeric', 'Part number'),
            (9,  28, 'alpha',   'Part description'),
            (29, 32, 'numeric', 'Warehouse location'),
            (33, 40, 'numeric', 'Quantity on hand'),
            (41, 48, 'numeric', 'Unit price (cents)'),
            (49, 54, 'numeric', 'Reorder point'),
            (55, 80, 'skip',    '(unused)'),
        ],
    },
    'grades': {
        'description': 'Student course grade record',
        'fields': [
            (1,  9,  'numeric', 'Student ID'),
            (10, 29, 'alpha',   'Last name'),
            (30, 44, 'alpha',   'First name'),
            (45, 49, 'alpha',   'Course code'),
            (50, 52, 'numeric', 'Grade (NNN)'),
            (53, 58, 'numeric', 'Credit hours (NN)'),
            (59, 80, 'skip',    '(unused)'),
        ],
    },
    'blank': {
        'description': 'Blank drum — no program card (defaults to alpha mode)',
        'fields': [],   # no punches; machine defaults to alpha throughout
    },
}


# ---------------------------------------------------------------------------
# Automated demo
# ---------------------------------------------------------------------------

def run_demo(machine: IBM026) -> None:
    """
    Punch a complete payroll record showing all three field types.
    Enables verbose relay tracing for the first field, then silences it.
    """
    print()
    print('╔══════════════════════════════════════════════════════════╗')
    print('║  IBM 026 Demo — punching one payroll record              ║')
    print('╚══════════════════════════════════════════════════════════╝')

    # Phase 1: employee number (verbose — shows relay chain)
    print('\n─── cols  1– 9  employee number  (numeric, 9 cols) ───')
    machine.verbose = True
    for ch in '000123456':
        machine.step(key=ch)

    # Phase 2: employee name (verbose off — one line per column)
    print('\n─── cols 10–29  employee name   (alpha,   20 cols) ───')
    machine.verbose = False
    for ch in 'MORRISON JAMES':
        r = machine.step(key=ch)
        print(f'  col {r["column"]:02d} [{r["mode"]:<8}]  '
              f'{ch!r}  →  punch {sorted(r["punched"])}  →  {r["char"]!r}')

    # Phase 3: operator presses SKIP mid-name field
    from_col = machine.column + 1
    print(f'\n─── SKIP key pressed at col {from_col} '
          f'(abandoning remaining name columns) ───')
    machine.verbose = True
    stopped = machine.press_skip_key()
    machine.verbose = False
    print(f'  → arrived at col {stopped} [{machine.mode}], '
          f'cols {from_col}–{stopped - 1} left blank')

    # Phase 4-6: remaining numeric fields
    remaining = [
        ('041425',   'cols 30–35  pay period date (numeric,  6 cols)'),
        ('4000000',  'cols 36–42  regular hours   (numeric,  7 cols)'),
        ('0000000',  'cols 43–49  overtime hours  (numeric,  7 cols)'),
        ('01500000', 'cols 50–57  pay rate        (numeric,  8 cols)'),
    ]
    for text, label in remaining:
        print(f'\n─── {label} ───')
        for ch in text:
            r = machine.step(key=ch)
            print(f'  col {r["column"]:02d} [{r["mode"]:<8}]  '
                  f'{ch!r}  →  punch {sorted(r["punched"])}  →  {r["char"]!r}')

    # Cols 58–80: drum-controlled auto-skip
    print('\n─── cols 58–80  drum auto-skip ───')
    while machine.skip_latch.NO and machine.column < NUM_COLS:
        r = machine.step()
        print(f'  col {r["column"]:02d} [SKIP]    (carriage advances, keyboard locked)')

    print('\n╔══════════════════════════════════════════════════════════╗')
    print('║  Completed card                                          ║')
    print('╚══════════════════════════════════════════════════════════╝')
    print(machine.read_card.display('Punched payroll card') if machine.read_card
          else machine.data_card.display('Punched payroll card'))


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

HELP_TEXT = """
IBM 026 Keypunch Simulator
══════════════════════════
Type a single character to punch it in the current column.
Type a multi-character string to punch each character in sequence.

Commands
────────
  SKIP          Skip to the next field boundary (like pressing the SKIP key)
  DUP           Duplicate one column from the card in the read station
  DUP <text>    Duplicate a whole run from the read station
  REL           Release (eject) the current card manually
  CARD          Display the card being punched
  LAST          Display the card in the read station
  DRUM          Display the program drum card
  RELAYS        Show all relay states
  STATUS        Show machine position and mode
  VERBOSE       Toggle relay tracing (on/off)
  LOAD <name>   Load a drum: payroll, inventory, grades, blank
  DEMO          Run the automated payroll demo
  HELP          Show this message
  QUIT          Exit
"""


def interactive_session(machine: IBM026, drum_name: str) -> None:
    print(HELP_TEXT)
    print(f'Drum loaded: {DRUMS[drum_name]["description"]}')
    _print_fields(DRUMS[drum_name]['fields'])
    print()
    print(machine.status())
    print()

    while True:
        # Auto-advance through skip columns before prompting
        if machine.skip_latch.NO:
            skipped_to = machine.flush_skips()
            if machine.column < NUM_COLS:
                print(f'  [auto-skip] → col {skipped_to}')
            else:
                print(f'  [auto-skip, card ejected]')

        col  = machine.column + 1
        mode = machine.mode
        try:
            line = input(f'col {col:02d} [{mode}]> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nBye.')
            break

        if not line:
            continue

        cmd = line.upper()

        if cmd in ('Q', 'QUIT', 'EXIT'):
            break

        elif cmd == 'HELP':
            print(HELP_TEXT)

        elif cmd == 'STATUS':
            print(machine.status())

        elif cmd == 'RELAYS':
            print(machine.relay_states())

        elif cmd == 'CARD':
            print(machine.data_card.display('Card being punched'))

        elif cmd == 'LAST':
            if machine.read_card:
                print(machine.read_card.display('Read station'))
            else:
                print('No card in read station yet.')

        elif cmd == 'DRUM':
            print(machine.drum.drum_card.display('Program drum card'))

        elif cmd == 'VERBOSE':
            machine.verbose = not machine.verbose
            print(f'Relay tracing: {"ON" if machine.verbose else "off"}')

        elif cmd == 'REL':
            machine._eject()
            print('[Card released → read station]')
            print(machine.status())

        elif cmd == 'DEMO':
            run_demo(machine)

        elif cmd.startswith('LOAD '):
            name = cmd[5:].strip().lower()
            if name not in DRUMS:
                print(f'Unknown drum.  Available: {", ".join(DRUMS)}')
            else:
                spec = DRUMS[name]
                new_drum = make_drum_card(spec['fields'])
                machine.drum = DrumSensor(new_drum)
                machine.column = 0
                machine.data_card = Card()
                machine.read_card = None
                machine.alpha_latch.set(True)
                machine._sense_and_latch()
                machine._configure_gates()
                print(f'Drum loaded: {spec["description"]}')
                _print_fields(spec['fields'])
                print(machine.status())

        elif cmd == 'SKIP':
            from_col = machine.column + 1
            stopped_at = machine.press_skip_key()
            if machine.column < NUM_COLS:
                print(f'  [SKIP key] col {from_col} → skipped to col {stopped_at} '
                      f'[{machine.mode}]')
            else:
                print(f'  [SKIP key] col {from_col} → end of card, ejected')
            print(machine.status())

        elif cmd == 'DUP':
            r = machine.step(dup=True)
            if r['action'] == 'dup_no_card':
                print('  No card in read station.')
            else:
                print(f'  dup col {r["column"]:02d}: '
                      f'rows {sorted(r["punched"])} → {r["char"]!r}')
                if r['ejected']:
                    print('  [Card ejected → read station]')
            print(machine.status())

        elif cmd.startswith('DUP '):
            # Dup a run of columns
            n = len(line) - 4
            for _ in range(n):
                if machine.skip_latch.NO:
                    machine.flush_skips()
                r = machine.step(dup=True)
                if r['action'] == 'dup_no_card':
                    print('  No card in read station.')
                    break
                print(f'  dup col {r["column"]:02d}: {r["char"]!r}')
                if r['ejected']:
                    print('  [Card ejected]')
                    break
            print(machine.status())

        elif len(line) == 1:
            _type_char(machine, line)

        else:
            # Multi-char string: type each character
            for ch in line:
                if machine.skip_latch.NO:
                    skipped_to = machine.flush_skips()
                    print(f'  [auto-skip] → col {skipped_to}')
                if machine.column >= NUM_COLS:
                    print('[Card ejected mid-string]')
                    break
                _type_char(machine, ch, brief=True)
            print(machine.status())


def _type_char(machine: IBM026, ch: str, brief: bool = False) -> None:
    try:
        r = machine.step(key=ch)
        if r['action'] == 'punch':
            if brief:
                print(f'  col {r["column"]:02d}  {ch!r}  →  {r["char"]!r}', end='')
                if r['ejected']:
                    print('  [ejected]', end='')
                print()
            else:
                print(f'  col {r["column"]:02d} [{r["mode"]:<8}]  '
                      f'key={ch!r}  '
                      f'bus={sorted(r["bus"])}  '
                      f'gated={sorted(r["gated"])}  '
                      f'punch={sorted(r["punched"])}  '
                      f'→ {r["char"]!r}')
                if r['ejected']:
                    print('  [Card ejected → read station]')
                print(machine.status())
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f'  Error: {exc}')


def _print_fields(fields: list) -> None:
    if not fields:
        print('  (no fields defined — machine defaults to alpha mode)')
        return
    print(f'  {"Cols":<12} {"Type":<10} Description')
    print(f'  {"────":<12} {"────":<10} ───────────')
    for start, end, ftype, *desc in fields:
        d = desc[0] if desc else ''
        print(f'  {start:>2}–{end:<8}  {ftype:<10} {d}')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='IBM 026 Keypunch relay logic simulator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Drums: ' + ', '.join(DRUMS),
    )
    parser.add_argument(
        '--drum', default='payroll', choices=list(DRUMS),
        help='Program drum to load (default: payroll)',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Trace relay states on every column cycle',
    )
    parser.add_argument(
        '--demo', action='store_true',
        help='Run the automated payroll demo and exit',
    )
    args = parser.parse_args()

    spec      = DRUMS[args.drum]
    drum_card = make_drum_card(spec['fields'])
    machine   = IBM026(drum_card, verbose=args.verbose)

    if args.demo:
        run_demo(machine)
    else:
        interactive_session(machine, args.drum)


if __name__ == '__main__':
    main()
