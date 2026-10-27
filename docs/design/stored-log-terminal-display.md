# Stored log and artifact display in the terminal dashboard

Status: accepted for the terminal dashboard view.

Stored run logs and artifact bytes are untrusted data relative to the
terminal. The dashboard shows them as text, not as terminal commands.

## Rules

1. Framework chrome owns terminal control sequences: clear, home, mouse
   enable and disable. Those writes stay outside `render()`.
2. Before a stored blob is shown, normalize newlines: CRLF and lone CR
   become LF. Interactive raw mode then converts each LF to CRLF once, so
   an already-CRLF page does not become CR CR LF.
3. ESC and other C0/C1 controls, except TAB and LF, become caret-form
   text (`^[` for ESC, `^A`…`^Z` for the matching controls, `^?` for DEL).
   They must not move the cursor, clear the screen, or change modes.
4. Printable Unicode, TAB, and LF stay unchanged so search and ordinary
   log reading keep working.
5. Redirected (non-TTY) output uses the same stored-text rules, so both
   paths show the same safe representation.

## Out of scope

Browser or network dashboard rendering, changing the log storage format,
and automatic redraw on resize without a key refresh.
