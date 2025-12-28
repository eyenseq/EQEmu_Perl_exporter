# Changelog

## v1.22 / 12-28-25

### Added

- Ctrl + Mouse Wheel and Ctrl +/- / Ctrl 0 font scaling for active editor panels
- Per-widget font zoom tracking with safe reset to original size
- High-contrast block highlighting with accent stripe and border (theme-aware)

### Changed

- Script tree block highlighting made significantly more visible while preserving readability
- Validation no longer regenerates full Perl under any circumstance
- Live Perl preview generation fully debounced and isolated from validation
- Tree diagnostics marking optimized to avoid repeated full-tree scans

### Fixed

- Barely-visible block styling in both light and dark themes
- Validation performance degradation on large scripts
- Font scaling inconsistencies between diagnostics, script tree, and editors

### Removed

- Implicit dependency on Perl generation during validation
- Redundant tree traversal during diagnostics application

---

## v1.21 / 12-27-25

### Added

- Stable block identifiers (`uid`) for reliable diagnostics, navigation, and undo/redo safety
- Hierarchy-aware EVENT validation (detects nested EVENT blocks and duplicate handlers)
- Infinite-loop detection heuristics for `WHILE` and `FOR` blocks
- Optional max-iteration safety guards for loop blocks
- Debounced Perl preview generation to keep diagnostics responsive during editing
- Bottom-docked Diagnostics panel to maximize script workspace
- Improved plugin template error reporting with plugin ID and missing parameter context

### Changed

- Diagnostics panel moved to a vertical splitter (bottom quarter layout by default)
- Perl preview generation decoupled from validation pass
- Whole-script linting made more conservative to reduce false positives
- RAW_PERL blocks no longer perform quote balancing checks
- ValidationIssue made immutable to prevent accidental mutation
- Plugin template rendering switched to placeholder-safe replacement only

### Fixed

- Crashes caused by `BLOCK_*` constants referenced before definition
- Fragile diagnostics mapping caused by use of `id(block)`
- False-positive quote and brace errors in RAW_PERL blocks
- Incorrect duplicate EVENT detection when blocks were nested
- Plugin render failures producing unclear or misleading diagnostics
- Excessive CPU usage from regenerating Perl on every keystroke

### Removed

- Reliance on object identity for block-to-diagnostic mapping
- Aggressive whole-script lint errors for heuristic-only checks

---

## v1.2 / 12-26-25

### Added

- Robust handling of custom (non-EVENT) Perl subs during import
- Raw Perl capture with brace balancing
- Theme-aware rendering supporting light and dark modes
- Improved script tree readability with human-readable block prefixes
- Diagnostics navigation improvements

### Changed

- Removed BLOCK_COMMAND entirely in favor of explicit logic
- Script tree rendering no longer caches text colors
- Improved false-positive prevention in diagnostics
- Simplified visual styling to favor readability

### Fixed

- False structural errors caused by helper subs
- Theme switching causing unreadable text
- Incorrect block closure during Perl import
- Multiple crashes related to missing visual helper methods

### Removed

- Command block abstraction
- Hardcoded color styling incompatible with light mode

---

## Previous Versions

Earlier versions relied more heavily on command abstractions and fixed dark-theme styling, which caused readability and import correctness issues. These have now been resolved.
