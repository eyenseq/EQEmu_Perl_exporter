# EQEmu Script Builder
A visual drag-and-drop quest-scripting IDE for EverQuest Emulator (EQEmu), featuring structured logic blocks, plugin-driven code generation, Perl import/export, persistent script state, and built-in syntax validation.

---

## 🚀 What’s New (Recent Updates)

This section highlights **recently added features**, what changed, and **how to use them**.

### 🔍 Diagnostics Panel (New)

A new right-side **Diagnostics Panel** provides live feedback while you build scripts. F8 to minimize or bring back.

**What it does**
- Live structural validation (missing EVENTs, bad timers, empty conditions)
- Live-generated Perl preview
- On-demand `perl -c` syntax checking

**How to use**
- Open automatically when editing a script
- Tabs:
  - **Validation** – errors, warnings, info
  - **Live Perl** – real-time generated output
  - **Perl -c** – click **Run perl -c** to validate syntax

---

### 🧠 Safer Plugin Template Rendering (Changed)

Plugin templates now use **strict placeholder substitution**.

**What changed**
- Only `{param}` placeholders are replaced
- All other braces `{}` are preserved
- Missing parameters raise clear validation errors
- No more escaping braces

---

### 🧪 Built-in Linting & Heuristics (New)

Beyond Perl syntax checking, the builder now performs **lightweight static analysis**.

**Detects**
- Unbalanced quotes, parentheses, and braces
- Empty IF / WHILE conditions
- Timers without `EVENT_TIMER`
- Suspicious tokens (`;;`, malformed `$timer eq "name"`)

---

### ⏱️ Smarter Timer Handling (Changed)

Timers are now context-aware.

**What changed**
- Warns if timers exist without an `EVENT_TIMER`
- Validates timer name and duration
- Auto-creates `EVENT_TIMER` handlers when possible

---

### 📦 Block Templates (New)

Save and reuse entire block trees.

**What it does**
- Store reusable logic patterns (hand-ins, greetings, timers)
- Templates saved to `block_templates.json`

---

## 📌 Overview

**EQEmu Script Builder** is a Python + PyQt6 application that allows EQEmu server developers to build NPC quest scripts using a **visual block-based editor** instead of manually writing Perl.

The tool supports:

- Drag-and-drop block reordering  
- Perl import with round-trip editing  
- Plugin templates with parameters  
- Raw Perl insertion  
- Syntax checking via `perl -c`  
- Undo/redo  
- Automatic state saving  

It is built specifically for EQEmu’s Perl quest system and significantly accelerates quest development.

---

## ✨ Features

### 🧩 Block-Based Script Editing

Build scripts visually using:

- `if`, `elsif`, `else`  
- `while`, `foreach`, `for`  
- `next`, `return`  
- Variable assignments  
- Bucket operations (`SetBucket`, `GetBucket`, `DeleteBucket`)  
- `quest::` function calls  
- Custom plugin blocks  
- Raw Perl blocks  

All blocks support **drag-and-drop** reordering and hierarchical nesting.

---

### 🎭 EVENT_* Support

Insert EVENT handlers quickly through the Events menu:

- `EVENT_SPAWN`
- `EVENT_SAY`
- `EVENT_TIMER`
- `EVENT_COMBAT`
- `EVENT_AGGRO`
- and many more…

Your preferred event list is saved persistently.

---

### 🔌 Plugin System

Create reusable code templates with automatically generated forms.

Each plugin defines:

- Name + unique ID  
- Perl template with `{param}` placeholders  
- Any number of parameters  
- Supported parameter types: string, integer, multiline  
- Alphabetized plugin lists in manager & editor  
- JSON persistence (`plugins.json`)  
- Safe rendering system  

#### Plugin Manager Enhancements

- **Double-click** parameter default values → opens a **multiline editor dialog**  
- Truncated previews in table view  
- Add/remove/edit parameters easily  

Plugins appear as selectable blocks in the palette and automatically render into Perl during export.

---

### 📥 Perl Import & Parsing

Open any `.pl` quest file and the builder reconstructs:

- `sub EVENT_*` blocks  
- Flow structures (`if`, loops, etc.)  
- Method calls  
- Bucket reads/writes  
- Comments  

Unrecognized lines are preserved as **Raw Perl blocks**, ensuring safe round-trip editing.

---

### 🧪 Perl Syntax Checking

Use:

Tools → Check Perl Syntax


The builder:

1. Generates Perl from your block structure  
2. Runs `perl -c`  
3. Displays warnings, errors, or **syntax OK**  

This prevents deploying broken scripts to your EQEmu server.

---

### 🎨 Themes

Two included UI themes:

- **Dark Theme** (default)  
- **Light Theme**

Theme selection persists after closing the app.

---

### 💾 Persistent State

Automatically stores:

- Script contents  
- Window size & position  
- Theme  
- Favorite events  
- Splitter layout  
- Active palette tab  

Everything restores on restart.

---

## 📦 Requirements

- Python **3.9+**
- **PyQt6**

Install dependencies:

```bash
pip install PyQt6
```

▶️ Running the Builder

Run the script:
```bash
python EQemu_script_builder.py
```

## UI Layout Overview

Left Panel: Flow blocks & EQEmu API method browser

Center Panel: Script tree (fully drag-and-drop enabled)

Right Panel: Dynamic property editor for selected block

🗂 Basic Workflow
1. Start or Load a Script
```
File → New Script

File → Open Perl…
```
2. Insert EVENT Blocks

Use the Events menu—EVENT_* blocks appear as top-level script elements.

3. Add Logic, Methods, and Plugins

Double-click method calls from the palette

Insert flow control blocks

Add plugin instances

Drag blocks anywhere to reorder or nest

4. Edit Block Properties

The properties panel updates based on block type:

Change expressions and arguments

Modify loop ranges

Edit plugin parameters

Insert multiline Raw Perl

Adjust timer durations

5. Use Plugins

Plugins → Manage Plugins

Inside the Plugin Manager:

Create new plugins

Define parameters

Edit Perl templates

Double-click defaults for multiline editing

Automatically saved into JSON

Alphabetically sorted for convenience

Plugin blocks instantly become available in the left palette.

6. Export Perl

Use:

File → Export Perl…


You can preview or save the generated .pl script.

7. Validate Syntax

Use:

Tools → Check Perl Syntax


Helps catch:

Missing braces

Typoed variables

Broken plugin templates

Unbalanced quotes

🧬 Plugin Template Format

Plugins use {name} placeholders:

quest::say("{message}");


Multiline templates supported:
```perl
if ({condition}) {
    $npc->Shout("{text}");
    quest::settimer("{timer}", {delay});
}
```

Parameters substitute safely during export.

📁 Stored Files

The application automatically creates and maintains:

File	Purpose
plugins.json	Plugin definitions
script_state.json	Last open script + UI layout
event_config.json	Your enabled EVENT_* shortcuts
🧯 Troubleshooting
Syntax errors on export

Use the syntax checker to pinpoint errors in plugin templates or raw Perl.

Plugin parameters not substituting

Check placeholder spelling in templates: {paramName} must match exactly.

Imported Perl doesn’t reconstruct perfectly

The parser is intentionally conservative; unsupported structures become Raw Perl blocks
