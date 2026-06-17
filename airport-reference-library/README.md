Airport Reference Library

Use this folder for airport-specific screenshots, notes, and station knowledge that should feed the flight briefing cards.

How to update
- Put new screenshots or PDFs into the matching airport folder under `sources/`.
- Update the station notes in [airport_briefing_notes.json](/Users/brianhope/Documents/New%20project%205/airport-reference-library/airport_briefing_notes.json).
- Use ICAO codes as the folder name, for example `YBBN`, `KSFO`, `RJAA`.

Current structure
- `airport_briefing_notes.json`: editable airport note database used by the brief generator.
- `YBBN/sources/`: original Brisbane source files used to build the current notes.

What gets added to briefs
- departure gate/ramp/taxi notes
- arrival gate/ramp/taxi notes
- airport-specific cautions that are operationally useful

Notes
- Keep entries short, operational, and reusable.
- If a source only applies to one phase, place it in `departure_notes` or `arrival_notes`.
