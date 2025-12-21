# Changelog (Rolling)

All notable changes to **EQEmu Script Builder** are documented here.
This project uses a rolling release model (no version numbers).

---

## 2025-12-20

### Added
- Live **Diagnostics Panel** with Validation, Live Perl preview, and Perl `-c`
- Real-time structural validation:
  - Missing or duplicate `EVENT_*` blocks
  - Timers without `EVENT_TIMER`
  - Empty IF / WHILE conditions
  - Plugin template rendering errors
- Heuristic linting for:
  - Unbalanced quotes, parentheses, and braces
  - Double semicolons (`;;`)
  - Dangling escape characters
  - Common malformed `$timer eq "name"` patterns
- **Block Template Registry** for reusable logic trees
- Expanded block types:
  - ARRAY / HASH ASSIGN
  - MY VAR / OUR VAR
  - NEXT with conditional suffix
  - Enhanced FOR / FOREACH blocks
- Comprehensive `EVENT_*` coverage (bot, client, item, spell, system)

### Changed
- Plugin template rendering now uses **safe regex substitution**
- Timer blocks validate names and durations
- Plugin Manager:
  - Alphabetical sorting
  - Multiline default editor via double-click
  - Safe default value storage
- Theme system refined with persistent dark/light modes
- Flow window is now alphabetized

### Fixed
- Plugin templates breaking due to brace misuse
- Silent failures when plugin parameters were missing
- Timers being set without warnings
- Loss of multiline defaults in plugin parameters

