import json
import os
import re
import collections
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from PyQt6 import QtWidgets, QtGui, QtCore
from functools import partial

# Matches {param_name} where param_name is a valid identifier
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

def render_plugin_template(template: str, params: Dict[str, Any]) -> str:
    """
    Replace {param} placeholders only.
    Leaves all other braces untouched (Perl blocks, JSON, etc. are safe).
    Raises a clear error if a placeholder exists but no param was provided.
    """

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in params:
            raise ValueError(f"Missing template param: {key}")
        val = params[key]
        return "" if val is None else str(val)

    return _PLACEHOLDER_RE.sub(repl, template)

@dataclass
class ValidationIssue:
    level: str   # "error" | "warn" | "info"
    message: str
    where: str = ""          # optional label/path
    block_ref: object = None # optional: direct Block reference for go-to

def _walk_blocks(blocks):
    for b in blocks:
        yield b
        for c in getattr(b, "children", []) or []:
            yield from _walk_blocks([c])

SEVERITY_ORDER = {
    "error": 3,
    "warn": 2,
    "info": 1,
}

def validate_blocks(blocks, plugin_registry) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    # -------------------------
    # Helpers (heuristics)
    # -------------------------
    def _count_unescaped(s: str, ch: str) -> int:
        if not s:
            return 0
        n = 0
        esc = False
        for c in s:
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == ch:
                n += 1
        return n

    def _unbalanced_quotes(s: str) -> bool:
        """
        Detect unbalanced string delimiters in Perl-like code.

        Rules:
        - Supports both ' and " as string delimiters
        - Ignores apostrophes inside the *other* quote type
        - Respects backslash escapes
        - Not a full Perl parser, but correct for quest scripts
        """
        in_single = False
        in_double = False
        esc = False

        for c in s:
            if esc:
                esc = False
                continue

            if c == "\\":
                esc = True
                continue

            if c == "'" and not in_double:
                in_single = not in_single
                continue

            if c == '"' and not in_single:
                in_double = not in_double
                continue

        # If we ended still inside a string → unbalanced
        return in_single or in_double

    def _balanced_pairs(s: str, open_ch: str, close_ch: str) -> tuple[bool, int]:
        """
        Returns (ok, delta) where delta>0 means more open than close,
        delta<0 means more close than open at some point.
        Ignores brackets inside quotes (roughly).
        """
        if not s:
            return True, 0
        stack = 0
        in_sq = False
        in_dq = False
        esc = False
        for c in s:
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == "'" and not in_dq:
                in_sq = not in_sq
                continue
            if c == '"' and not in_sq:
                in_dq = not in_dq
                continue
            if in_sq or in_dq:
                continue

            if c == open_ch:
                stack += 1
            elif c == close_ch:
                stack -= 1
                if stack < 0:
                    return False, stack
        return (stack == 0), stack

    def _has_suspicious_tokens(s: str) -> list[str]:
        """
        Extra sanity checks: returns list of warnings strings.
        """
        out = []
        if not s:
            return out

        # Common "oops" in Perl
        if "$timer eq" in s and '"' in s and "$timer eq \"" in s and '"' not in s.split("$timer eq", 1)[1]:
            out.append("Possible malformed $timer eq \"name\" comparison.")

        # double semicolons aren't fatal but often a typo
        if ";;" in s:
            out.append("Contains ';;' (double semicolon).")

        # a dangling backslash at end of line
        for line in s.splitlines():
            if line.rstrip().endswith("\\"):
                out.append("Line ends with '\\' (possible accidental escape).")
                break

        return out

    def _walk_blocks(bs):
        for b in bs:
            yield b
            for c in getattr(b, "children", []) or []:
                yield from _walk_blocks([c])
    def lint_text(
        text: str,
        where_label: str,
        severity: str = "warn",
        block_ref=None,
        check_quotes: bool = True,
        check_parens: bool = True,
        check_braces: bool = True,
    ):
        if not text:
            return

        # Quotes
        if check_quotes and _unbalanced_quotes(text):
            issues.append(ValidationIssue(severity, "Unbalanced quotes (missing \" or ').", where_label, block_ref=block_ref))

        # Parentheses
        if check_parens:
            ok, delta = _balanced_pairs(text, "(", ")")
            if not ok:
                msg = "Too many ')' (parentheses close before open)." if delta < 0 else "Unbalanced parentheses (missing ')')."
                issues.append(ValidationIssue(severity, msg, where_label, block_ref=block_ref))
            elif delta != 0:
                issues.append(ValidationIssue(severity, "Unbalanced parentheses (missing ')').", where_label, block_ref=block_ref))

        # Braces
        if check_braces:
            ok, delta = _balanced_pairs(text, "{", "}")
            if not ok:
                msg = "Too many '}' (brace close before open)." if delta < 0 else "Unbalanced braces (missing '}')."
                issues.append(ValidationIssue(severity, msg, where_label, block_ref=block_ref))
            elif delta != 0:
                issues.append(ValidationIssue(severity, "Unbalanced braces (missing '}').", where_label, block_ref=block_ref))

        # Extra cheap heuristics
        for w in _has_suspicious_tokens(text):
            issues.append(ValidationIssue("info", w, where_label, block_ref=block_ref))

    # -------------------------
    # 1) Basic structure checks
    # -------------------------
    event_blocks = [b for b in blocks if b.type == BLOCK_EVENT]
    if not event_blocks:
        issues.append(ValidationIssue("warn", "No EVENT block found (script will do nothing).", "root"))

    # -------------------------
    # 2) Duplicate top-level EVENT_* blocks
    # -------------------------
    counts = {}
    for b in blocks:
        if b.type == BLOCK_EVENT:
            ev = (b.params.get("event_name") or "").strip()
            if ev:
                counts[ev] = counts.get(ev, 0) + 1
    for ev, n in counts.items():
        if n > 1:
            issues.append(ValidationIssue(
                "warn",
                f"Duplicate top-level handler: {ev} appears {n} times (often accidental).",
                ev
            ))

    # -------------------------
    # 3) Plugin template checks
    # -------------------------
    for b in _walk_blocks(blocks):
        if b.type != BLOCK_PLUGIN:
            continue

        pid = b.params.get("plugin_id")
        if not pid:
            issues.append(ValidationIssue("error", "Plugin block has no plugin selected.", b.label, block_ref=b))
            continue

        pdef = plugin_registry.get(pid)
        if not pdef:
            issues.append(ValidationIssue("error", f"Unknown plugin_id '{pid}' (not found in plugins.json).", b.label, block_ref=b))
            continue

        plugin_params = b.params.get("plugin_params", {}) or {}
        try:
            _ = render_plugin_template(pdef.perl_template, plugin_params)
        except Exception as e:
            issues.append(ValidationIssue("error", f"Plugin template render failed: {e}", b.label, block_ref=b))

    # -------------------------
    # 4) Timer checks
    # -------------------------
    timer_sets = set()
    for b in _walk_blocks(blocks):
        if b.type == BLOCK_TIMER:
            name = (b.params.get("name") or "").strip()
            secs = b.params.get("seconds", None)
            if not name:
                issues.append(ValidationIssue("error", "Timer block missing timer name.", b.label, block_ref=b))
            else:
                timer_sets.add(name)
            try:
                if secs is None or int(secs) < 0:
                    issues.append(ValidationIssue("error", "Timer seconds must be >= 0.", b.label, block_ref=b))
            except Exception:
                issues.append(ValidationIssue("error", "Timer seconds is not an integer.", b.label, block_ref=b))

    has_event_timer = any(b.type == BLOCK_EVENT and b.params.get("event_name") == "EVENT_TIMER" for b in blocks)
    if timer_sets and not has_event_timer:
        issues.append(ValidationIssue("warn", "You set timers but have no EVENT_TIMER handler.", "root"))

    # -------------------------
    # 5) Per-block syntax-ish lint
    # -------------------------
    for b in _walk_blocks(blocks):

        # RAW PERL: may open { or ( and close later elsewhere
        if b.type == BLOCK_RAW_PERL:
            code = (b.params.get("code") or "")
            lint_text(
                code,
                b.label,
                severity="warn",
                block_ref=b,
                check_braces=False,
                check_parens=False,
            )
            continue

        # IF / WHILE conditions
        if b.type in (BLOCK_IF, BLOCK_WHILE):
            expr = (b.params.get("expr") or "").strip()
            if not expr:
                issues.append(
                    ValidationIssue("error", "Empty condition expression.", b.label, block_ref=b)
                )
            else:
                lint_text(expr, b.label, severity="warn", block_ref=b)

        # Quest calls (args)
        if b.type == BLOCK_QUEST_CALL:
            args = (b.params.get("args") or "")
            lint_text(args, b.label, severity="warn", block_ref=b)

    # -------------------------
    # 6) Whole-script lint (Option 1)
    # -------------------------
    try:
        perl = generate_perl(blocks, plugin_registry)
    except Exception as e:
        issues.append(
            ValidationIssue("error", f"Failed to generate Perl for linting: {e}", "root")
        )
        return issues

    # Whole-script quotes
    if _unbalanced_quotes(perl):
        issues.append(
            ValidationIssue("error", 'Whole script: unbalanced quotes.', "root")
        )

    # Whole-script parentheses
    ok, delta = _balanced_pairs(perl, "(", ")")
    if not ok:
        msg = "Whole script: too many ')'." if delta < 0 else "Whole script: missing ')'."
        issues.append(ValidationIssue("error", msg, "root"))
    elif delta != 0:
        issues.append(
            ValidationIssue("error", "Whole script: unbalanced parentheses.", "root")
        )

    # Whole-script braces
    ok, delta = _balanced_pairs(perl, "{", "}")
    if not ok:
        msg = "Whole script: too many '}'." if delta < 0 else "Whole script: missing '}'."
        issues.append(ValidationIssue("error", msg, "root"))
    elif delta != 0:
        issues.append(
            ValidationIssue("error", "Whole script: unbalanced braces.", "root")
        )
    
    return issues
    
def apply_dark_theme(app: QtWidgets.QApplication):
    # Use Fusion as a base
    app.setStyle("Fusion")

    # --- Dark palette ---
    palette = QtGui.QPalette()

    window      = QtGui.QColor(32, 33, 36)   # background
    base        = QtGui.QColor(41, 42, 45)   # widget background
    alt_base    = QtGui.QColor(48, 49, 52)
    text        = QtGui.QColor(241, 243, 244)
    disabled    = QtGui.QColor(130, 134, 139)
    highlight   = QtGui.QColor(138, 180, 248)
    highlight_d = QtGui.QColor(96, 125, 179)
    border      = QtGui.QColor(60, 64, 67)

    palette.setColor(QtGui.QPalette.ColorRole.Window, window)
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, text)
    palette.setColor(QtGui.QPalette.ColorRole.Base, base)
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QtGui.QPalette.ColorRole.Text, text)
    palette.setColor(QtGui.QPalette.ColorRole.Button, base)
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, text)
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, base)
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(0, 0, 0))

    palette.setColor(QtGui.QPalette.ColorGroup.Disabled,
                     QtGui.QPalette.ColorRole.Text, disabled)
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled,
                     QtGui.QPalette.ColorRole.ButtonText, disabled)

    app.setPalette(palette)

    # --- Global stylesheet for a more modern look ---
    app.setStyleSheet("""
    QMainWindow {
        background-color: #202124;
    }

    QMenuBar {
        background-color: #202124;
        color: #f1f3f4;
        padding: 4px;
    }
    QMenuBar::item {
        background: transparent;
        padding: 4px 10px;
    }
    QMenuBar::item:selected {
        background: #3c4043;
        border-radius: 4px;
    }

    QMenu {
        background-color: #2b2d30;
        color: #f1f3f4;
        border: 1px solid #3c4043;
    }
    QMenu::item:selected {
        background-color: #3c4043;
    }

    QTabWidget::pane {
        border: 1px solid #3c4043;
        border-radius: 6px;
        margin-top: -1px;
    }
    QTabBar::tab {
        background: #2b2d30;
        color: #f1f3f4;
        padding: 6px 12px;
        border: 1px solid #3c4043;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        margin-right: 2px;
    }
    QTabBar::tab:selected {
        background: #3c4043;
    }
    QTabBar::tab:hover {
        background: #34373b;
    }

    QListWidget, QTreeWidget, QTableWidget,
    QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
        background-color: #2b2d30;
        color: #f1f3f4;
        border: 1px solid #3c4043;
        border-radius: 4px;
        selection-background-color: #3c4043;
        selection-color: #f1f3f4;
    }
    QTreeWidget::item:selected,
    QListWidget::item:selected {
        background-color: #3c4043;
        color: #f1f3f4;
    }

    QLabel {
        color: #e8eaed;
    }

    QPushButton {
        background-color: #3c4043;
        color: #f1f3f4;
        border: 1px solid #5f6368;
        border-radius: 6px;
        padding: 5px 12px;
    }
    QPushButton:hover {
        background-color: #4b4f54;
    }
    QPushButton:pressed {
        background-color: #5f6368;
    }
    QPushButton:disabled {
        background-color: #2b2d30;
        color: #80868b;
        border-color: #3c4043;
    }

    QHeaderView::section {
        background-color: #2b2d30;
        color: #e8eaed;
        padding: 4px;
        border: 1px solid #3c4043;
    }

    QScrollBar:vertical {
        background: #202124;
        width: 10px;
        margin: 0px;
    }
    QScrollBar::handle:vertical {
        background: #5f6368;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical:hover {
        background: #9aa0a6;
    }
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {
        height: 0;
    }

    QScrollBar:horizontal {
        background: #202124;
        height: 10px;
        margin: 0px;
    }
    QScrollBar::handle:horizontal {
        background: #5f6368;
        min-width: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:horizontal:hover {
        background: #9aa0a6;
    }
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {
        width: 0;
    }
    """)
    font = QtGui.QFont("Segoe UI", 9)
    app.setFont(font)


def apply_light_theme(app: QtWidgets.QApplication):
    """Simple light theme: white background, black text."""
    app.setStyle("Fusion")

    palette = QtGui.QPalette()

    # Basic white/black colors
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("white"))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor("black"))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("white"))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(240, 240, 240))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("black"))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(240, 240, 240))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("black"))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(255, 255, 220))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor("black"))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(76, 163, 224))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("white"))

    # Disabled state
    disabled_text = QtGui.QColor(160, 160, 160)
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled,
                     QtGui.QPalette.ColorRole.Text, disabled_text)
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled,
                     QtGui.QPalette.ColorRole.ButtonText, disabled_text)

    app.setPalette(palette)

    # Clear dark stylesheet so light palette isn't overridden
    app.setStyleSheet("")
    font = QtGui.QFont("Segoe UI", 9)
    app.setFont(font)


# Backwards-compatible alias so existing call still works
def apply_modern_theme(app: QtWidgets.QApplication):
    apply_dark_theme(app)


NPC_EVENTS = sorted({
    # --- Bot / damage / timers / misc ---
    "EVENT_AGGRO_SAY",
    "EVENT_DAMAGE_GIVEN",
    "EVENT_DAMAGE_TAKEN",
    "EVENT_ENTITY_VARIABLE_DELETE",
    "EVENT_ENTITY_VARIABLE_SET",
    "EVENT_ENTITY_VARIABLE_UPDATE",
    "EVENT_EQUIP_ITEM_BOT",
    "EVENT_LEVEL_DOWN",
    "EVENT_LEVEL_UP",
    "EVENT_PAYLOAD",
    "EVENT_SPELL_BLOCKED",
    "EVENT_TARGET_CHANGE",
    "EVENT_TIMER_PAUSE",
    "EVENT_TIMER_RESUME",
    "EVENT_TIMER_START",
    "EVENT_TIMER_STOP",
    "EVENT_UNEQUIP_ITEM_BOT",
    "EVENT_USE_SKILL",

    # --- Item / loot events ---
    "EVENT_AUGMENT_INSERT",
    "EVENT_AUGMENT_ITEM",
    "EVENT_AUGMENT_REMOVE",
    "EVENT_DESTROY_ITEM",
    "EVENT_DROP_ITEM",
    "EVENT_EQUIP_ITEM",
    "EVENT_ITEM_CLICK",
    "EVENT_ITEM_CLICK_CAST",
    "EVENT_ITEM_ENTER_ZONE",
    "EVENT_LOOT",
    "EVENT_SCALE_CALC",
    "EVENT_UNAUGMENT_ITEM",
    "EVENT_UNEQUIP_ITEM",
    "EVENT_WEAPON_PROC",

    # --- NPC / proximity / pathing ---
    "EVENT_AGGRO",
    "EVENT_ENTER_AREA",
    "EVENT_EXIT",
    "EVENT_FEIGN_DEATH",
    "EVENT_HATE_LIST",
    "EVENT_HP",
    "EVENT_KILLED_MERIT",
    "EVENT_LEAVE_AREA",
    "EVENT_LOOT_ADDED",
    "EVENT_LOOT_ZONE",
    "EVENT_PROXIMITY_SAY",
    "EVENT_SPAWN",
    "EVENT_SPAWN_ZONE",
    "EVENT_TASKACCEPTED",
    "EVENT_TICK",
    "EVENT_WAYPOINT_ARRIVE",
    "EVENT_WAYPOINT_DEPART",

    # --- Client / AA / task / currency / misc ---
    "EVENT_AA_BUY",
    "EVENT_AA_EXP_GAIN",
    "EVENT_AA_GAIN",
    "EVENT_AA_LOSS",
    "EVENT_ALT_CURRENCY_LOSS",
    "EVENT_ALT_CURRENCY_MERCHANT_BUY",
    "EVENT_ALT_CURRENCY_MERCHANT_SELL",
    "EVENT_AUGMENT_INSERT_CLIENT",
    "EVENT_AUGMENT_REMOVE_CLIENT",
    "EVENT_BOT_COMMAND",
    "EVENT_BOT_CREATE",
    "EVENT_CLICKDOOR",
    "EVENT_CLICK_OBJECT",
    "EVENT_COMBINE",
    "EVENT_COMBINE_FAILURE",
    "EVENT_COMBINE_SUCCESS",
    "EVENT_COMBINE_VALIDATE",
    "EVENT_COMMAND",
    "EVENT_CONNECT",
    "EVENT_CONSIDER",
    "EVENT_CONSIDER_CORPSE",
    "EVENT_CRYSTAL_GAIN",
    "EVENT_CRYSTAL_LOSS",
    "EVENT_DESTROY_ITEM_CLIENT",
    "EVENT_DISCONNECT",
    "EVENT_DISCOVER_ITEM",
    "EVENT_DROP_ITEM_CLIENT",
    "EVENT_DUEL_LOSE",
    "EVENT_DUEL_WIN",
    "EVENT_ENTER",
    "EVENT_ENTER_ZONE",
    "EVENT_ENVIROMENTAL_DAMAGE",
    "EVENT_EQUIP_ITEM_CLIENT",
    "EVENT_EXP_GAIN",
    "EVENT_FISH_FAILURE",
    "EVENT_FISH_START",
    "EVENT_FISH_SUCCESS",
    "EVENT_FORAGE_FAILURE",
    "EVENT_FORAGE_SUCCESS",
    "EVENT_GM_COMMAND",
    "EVENT_GROUP_CHANGE",
    "EVENT_INSPECT",
    "EVENT_ITEM_CLICK_CAST_CLIENT",
    "EVENT_ITEM_CLICK_CLIENT",
    "EVENT_LANGUAGE_SKILL_UP",
    "EVENT_MEMORIZE_SPELL",
    "EVENT_MERCHANT_BUY",
    "EVENT_MERCHANT_SELL",
    "EVENT_PLAYER_PICKUP",
    "EVENT_READ_ITEM",
    "EVENT_RESPAWN",
    "EVENT_SCRIBE_SPELL",
    "EVENT_SKILL_UP",
    "EVENT_TASK_BEFORE_UPDATE",
    "EVENT_TASK_COMPLETE",
    "EVENT_TASK_FAIL",
    "EVENT_TASK_STAGE_COMPLETE",
    "EVENT_TASK_UPDATE",
    "EVENT_UNEQUIP_ITEM_CLIENT",
    "EVENT_UNHANDLED_OPCODE",
    "EVENT_UNMEMORIZE_SPELL",
    "EVENT_UNSCRIBE_SPELL",
    "EVENT_WARP",
    "EVENT_ZONE",
    "EVENT_SPELL_EFFECT_BOT",
    "EVENT_SPELL_EFFECT_BUFFZ_TIC_BOT",
    "EVENT_SPELL_EFFECT_BUFF_TIC_CLIENT",
    "EVENT_SPELL_EFFECT_BUFF_TIC_NPC",
    "EVENT_SPELL_EFFECT_CLIENT",
    "EVENT_SPELL_EFFECT_NPC",
    "EVENT_SPELL_EFFECT_TRANSLOCAT_COMPLETE",
    "EVENT_SPELL_FADE",

    # --- Core quest events ---
    "EVENT_ATTACK",
    "EVENT_CAST",
    "EVENT_CAST_BEGIN",
    "EVENT_CAST_ON",
    "EVENT_COMBAT",
    "EVENT_DEATH",
    "EVENT_DEATH_COMPLETE",
    "EVENT_DESPAWN",
    "EVENT_ITEM",
    "EVENT_NPC_SLAY",
    "EVENT_POPUPRESPONSE",
    "EVENT_SAY",
    "EVENT_SIGNAL",
    "EVENT_SLAY",
    "EVENT_TIMER",
})

# Default “common” events to show in the Events menu
DEFAULT_COMMON_EVENTS = [
    "EVENT_SPAWN",
    "EVENT_SAY",
    "EVENT_ITEM",
    "EVENT_SIGNAL",
    "EVENT_TIMER",
    "EVENT_HP",
    "EVENT_COMBAT",
    "EVENT_AGGRO",
    "EVENT_DEATH",
    "EVENT_ENTER",
    "EVENT_EXIT",
    "EVENT_WAYPOINT_ARRIVE",
    "EVENT_WAYPOINT_DEPART",
]

EVENT_CONFIG_FILE = "events.json"
SCRIPT_STATE_FILE = "script_state.json"



# -------------------------
# Data model
# -------------------------

BLOCK_EVENT       = "event"
BLOCK_IF          = "if"
BLOCK_ELSIF       = "elsif"
BLOCK_ELSE        = "else"
BLOCK_WHILE       = "while"
BLOCK_RETURN      = "return"
BLOCK_COMMENT     = "comment"
BLOCK_SET_VAR     = "set_var"
BLOCK_SET_BUCKET  = "set_bucket"
BLOCK_GET_BUCKET  = "get_bucket"
BLOCK_DELETE_BUCKET = "delete_bucket"
BLOCK_TIMER       = "timer"
BLOCK_FOR         = "for"
BLOCK_FOREACH     = "foreach"
BLOCK_PLUGIN      = "plugin"
BLOCK_RAW_PERL    = "raw_perl"
BLOCK_METHOD      = "method_call"
BLOCK_QUEST_CALL  = "quest_call"
BLOCK_MY_VAR      = "my_var"
BLOCK_OUR_VAR     = "our_var"
BLOCK_NEXT        = "next"
BLOCK_ARRAY_ASSIGN = "array_assign"

@dataclass
class Block:
    type: str
    label: str
    params: Dict[str, Any] = field(default_factory=dict)
    children: List["Block"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "params": self.params,
            "children": [c.to_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Block":
        b = Block(
            type=data["type"],
            label=data.get("label", data["type"]),
            params=data.get("params", {}),
            children=[]
        )
        for child in data.get("children", []):
            b.children.append(Block.from_dict(child))
        return b


@dataclass
class PluginDef:
    plugin_id: str
    name: str
    category: str
    perl_template: str
    params: List[Dict[str, Any]]  # [{"name", "label", "type", "default"}, ...]


@dataclass
class MethodDef:
    category: str  # e.g. "CLIENT"
    var: str       # e.g. "$client"
    name: str      # e.g. "Message"
    args: str      # e.g. "int32 type, std::string message"


# -------------------------
# Load methods from API reference
# -------------------------

def load_api_methods() -> Dict[str, List[MethodDef]]:
    """
    Parse the uploaded Perl Quest API reference to build a registry of methods.
    Looks for 'perl_reference.txt' or 'perl_reference (1).txt' in the current dir.
    """
    methods_by_cat: Dict[str, List[MethodDef]] = collections.defaultdict(list)
    candidates = ["perl_reference.txt", "perl_reference (1).txt"]
    ref_path = None
    for p in candidates:
        if os.path.exists(p):
            ref_path = p
            break

    if ref_path is None:
        return methods_by_cat

    cat_header_re = re.compile(r'^\[(.+?) METHODS\]')
    method_re = re.compile(r'^\$(\w+)->(\w+)\(([^)]*)\);')

    current_cat: Optional[str] = None

    with open(ref_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            m = cat_header_re.match(line)
            if m:
                current_cat = m.group(1).strip().upper()
                continue
            if not current_cat:
                continue
            mm = method_re.match(line)
            if mm:
                var, name, args = mm.groups()
                args = args.strip()
                var_str = f"${var}"  # 'client' -> '$client'
                methods_by_cat[current_cat].append(
                    MethodDef(
                        category=current_cat,
                        var=var_str,
                        name=name,
                        args=args
                    )
                )

    return methods_by_cat

@dataclass
class BlockTemplateDef:
    template_id: str
    name: str
    root_block: Dict[str, Any]  # Block.to_dict()

class BlockTemplateRegistry:
    def __init__(self, path: str = "block_templates.json"):
        self.path = path
        self.templates: Dict[str, BlockTemplateDef] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.templates = {}
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.templates = {}
        for t in data.get("templates", []):
            self.templates[t["template_id"]] = BlockTemplateDef(
                template_id=t["template_id"],
                name=t.get("name", t["template_id"]),
                root_block=t["root_block"],
            )

    def delete(self, template_id: str) -> bool:
        if template_id in self.templates:
            del self.templates[template_id]
            self.save()
            return True
        return False

    def save(self):
        data = {"templates": [
            {"template_id": t.template_id, "name": t.name, "root_block": t.root_block}
            for t in self.templates.values()
        ]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def list_templates(self) -> List[BlockTemplateDef]:
        return sorted(self.templates.values(), key=lambda t: t.name.lower())

# -------------------------
# Plugin Manager (JSON-backed)
# -------------------------

class PluginRegistry:
    def __init__(self, path: str = "plugins.json"):
        self.path = path
        self.plugins: Dict[str, PluginDef] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            # Seed with an example plugin so UI isn't empty
            example = PluginDef(
                plugin_id="say_to_client",
                name="Say To Client",
                category="Chat",
                perl_template='plugin::SayToClient($client, "{message}");',
                params=[
                    {
                        "name": "message",
                        "label": "Message",
                        "type": "str",
                        "default": "Hello there!"
                    }
                ]
            )
            self.plugins[example.plugin_id] = example
            self.save()
            return

        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.plugins.clear()
        for pd in data.get("plugins", []):
            self.plugins[pd["plugin_id"]] = PluginDef(
                plugin_id=pd["plugin_id"],
                name=pd["name"],
                category=pd.get("category", "General"),
                perl_template=pd["perl_template"],
                params=pd.get("params", []),
            )

    def save(self):
        data = {
            "plugins": [
                {
                    "plugin_id": p.plugin_id,
                    "name": p.name,
                    "category": p.category,
                    "perl_template": p.perl_template,
                    "params": p.params,
                }
                for p in self.plugins.values()
            ]
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def list_plugins(self) -> List[PluginDef]:
        # Alphabetize by plugin name (case-insensitive)
        return sorted(self.plugins.values(), key=lambda p: p.name.lower())


    def get(self, plugin_id: str) -> Optional[PluginDef]:
        return self.plugins.get(plugin_id)

class PluginManagerDialog(QtWidgets.QDialog):
    """
    Plugin editor:
    - List on the left
    - Basic fields on the right
    - Params edited via a QTableWidget (no JSON required)
    """
    def __init__(self, registry: PluginRegistry, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plugin Manager")
        self.registry = registry
        self.current: Optional[PluginDef] = None

        self.resize(800, 400)
        layout = QtWidgets.QHBoxLayout(self)

        # Left: list
        self.list_widget = QtWidgets.QListWidget()
        layout.addWidget(self.list_widget, 1)

        # Right: form
        form_widget = QtWidgets.QWidget()
        form_layout = QtWidgets.QFormLayout(form_widget)

        self.edit_id = QtWidgets.QLineEdit()
        self.edit_name = QtWidgets.QLineEdit()
        self.edit_category = QtWidgets.QLineEdit()
        self.edit_template = QtWidgets.QPlainTextEdit()

        # Table for params: rows = params, cols = (name, label, type, default)
        self.params_table = QtWidgets.QTableWidget(0, 4)
        self.params_table.setHorizontalHeaderLabels(["Name", "Label", "Type", "Default"])
        self.params_table.horizontalHeader().setStretchLastSection(True)
        self.params_table.cellDoubleClicked.connect(self.on_param_cell_double_clicked)

        form_layout.addRow("Plugin ID", self.edit_id)
        form_layout.addRow("Name", self.edit_name)
        form_layout.addRow("Category", self.edit_category)
        form_layout.addRow("Perl Template", self.edit_template)
        form_layout.addRow("Parameters", self.params_table)

        # Buttons for params
        btn_add_param = QtWidgets.QPushButton("Add Param")
        btn_del_param = QtWidgets.QPushButton("Remove Selected Param")
        param_btn_box = QtWidgets.QHBoxLayout()
        param_btn_box.addWidget(btn_add_param)
        param_btn_box.addWidget(btn_del_param)
        form_layout.addRow(param_btn_box)

        btn_add_param.clicked.connect(self.on_add_param)
        btn_del_param.clicked.connect(self.on_delete_param)

        btn_save = QtWidgets.QPushButton("Save Plugin")
        btn_new = QtWidgets.QPushButton("New")
        btn_delete = QtWidgets.QPushButton("Delete")

        btn_box = QtWidgets.QHBoxLayout()
        btn_box.addWidget(btn_new)
        btn_box.addWidget(btn_delete)
        btn_box.addWidget(btn_save)
        form_layout.addRow(btn_box)

        layout.addWidget(form_widget, 2)

        # Signals
        self.list_widget.currentItemChanged.connect(self.on_list_selected)
        btn_new.clicked.connect(self.on_new)
        btn_delete.clicked.connect(self.on_delete)
        btn_save.clicked.connect(self.on_save)

        self.refresh_list()

    # --- Params helpers ---
    def _preview_default(self, value: Any, max_len: int = 40) -> str:
        """
        Show a short, single-line preview of the default value.
        Newlines become '⏎', and long text is truncated.
        Works for str, int, etc. by stringifying first.
        """
        if value is None:
            return ""

        text = str(value)
        if not text:
            return ""

        one_line = text.replace("\n", "⏎")
        if len(one_line) > max_len:
            one_line = one_line[: max_len - 3] + "..."
        return one_line
    
    def clear_params_table(self):
        self.params_table.setRowCount(0)

    def load_params_into_table(self, params_list: List[Dict[str, Any]]):
        self.clear_params_table()
        for p in params_list:
            row = self.params_table.rowCount()
            self.params_table.insertRow(row)

            def mk_item(value: Any) -> QtWidgets.QTableWidgetItem:
                item = QtWidgets.QTableWidgetItem(str(value))
                return item

            name_item    = mk_item(p.get("name", ""))
            label_item   = mk_item(p.get("label", p.get("name", "")))
            type_item    = mk_item(p.get("type", "str"))

            full_default = p.get("default", "")
            default_item = mk_item(self._preview_default(full_default))
            # store full default in UserRole so we don't lose newlines
            default_item.setData(QtCore.Qt.ItemDataRole.UserRole, full_default)

            self.params_table.setItem(row, 0, name_item)
            self.params_table.setItem(row, 1, label_item)
            self.params_table.setItem(row, 2, type_item)
            self.params_table.setItem(row, 3, default_item)


    def collect_params_from_table(self) -> List[Dict[str, Any]]:
        params: List[Dict[str, Any]] = []
        for row in range(self.params_table.rowCount()):
            name_item    = self.params_table.item(row, 0)
            label_item   = self.params_table.item(row, 1)
            type_item    = self.params_table.item(row, 2)
            default_item = self.params_table.item(row, 3)

            name = (name_item.text().strip() if name_item else "")
            if not name:
                continue  # skip blank rows

            label = (label_item.text().strip() if label_item else name)
            ptype = (type_item.text().strip() if type_item else "str")

            # pull full default from UserRole if it exists, otherwise use cell text
            if default_item:
                stored = default_item.data(QtCore.Qt.ItemDataRole.UserRole)
                if stored is not None:
                    default = str(stored)
                else:
                    default = default_item.text()
            else:
                default = ""

            params.append({
                "name": name,
                "label": label,
                "type": ptype,
                "default": default,
            })
        return params

    def on_param_cell_double_clicked(self, row: int, column: int):
        """
        If the user double-clicks the Default cell, open a popup dialog
        with a multi-line editor for the default value.
        """
        # Only care about the "Default" column (index 3)
        if column != 3:
            return

        item = self.params_table.item(row, column)
        if item is None:
            item = QtWidgets.QTableWidgetItem("")
            self.params_table.setItem(row, column, item)

        # Get the full existing default from UserRole if present
        existing = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if existing is None:
            existing = item.text()

        # Build the dialog
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Edit Default Value (multiline)")
        dlg.resize(500, 300)

        vbox = QtWidgets.QVBoxLayout(dlg)

        info = QtWidgets.QLabel(
            "Enter the default value for this parameter.\n"
            "This can span multiple lines."
        )
        info.setWordWrap(True)
        vbox.addWidget(info)

        edit = QtWidgets.QPlainTextEdit()
        edit.setPlainText(str(existing) if existing is not None else "")
        font = QtGui.QFont("Consolas", 9)
        edit.setFont(font)
        vbox.addWidget(edit)

        btn_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        vbox.addWidget(btn_box)

        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)

        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new_val = edit.toPlainText()
            # Store full value in UserRole
            item.setData(QtCore.Qt.ItemDataRole.UserRole, new_val)
            # Show a short preview in the cell
            item.setText(self._preview_default(new_val))



    def on_add_param(self):
        row = self.params_table.rowCount()
        self.params_table.insertRow(row)
        self.params_table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"param{row+1}"))
        self.params_table.setItem(row, 1, QtWidgets.QTableWidgetItem(""))
        self.params_table.setItem(row, 2, QtWidgets.QTableWidgetItem("str"))
        self.params_table.setItem(row, 3, QtWidgets.QTableWidgetItem(""))

    def on_delete_param(self):
        row = self.params_table.currentRow()
        if row >= 0:
            self.params_table.removeRow(row)

    # --- Plugin list / CRUD ---

    def refresh_list(self):
        self.list_widget.clear()
        for p in self.registry.list_plugins():
            item = QtWidgets.QListWidgetItem(f"{p.name} ({p.plugin_id})")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, p.plugin_id)
            self.list_widget.addItem(item)

    def load_plugin_into_form(self, plugin: PluginDef):
        self.edit_id.setText(plugin.plugin_id)
        self.edit_name.setText(plugin.name)
        self.edit_category.setText(plugin.category)
        self.edit_template.setPlainText(plugin.perl_template)
        self.load_params_into_table(plugin.params)

    def on_list_selected(self, current, _previous):
        if not current:
            self.current = None
            return
        plugin_id = current.data(QtCore.Qt.ItemDataRole.UserRole)
        plugin = self.registry.get(plugin_id)
        if plugin:
            self.current = plugin
            self.load_plugin_into_form(plugin)

    def on_new(self):
        self.current = None
        self.edit_id.clear()
        self.edit_name.clear()
        self.edit_category.clear()
        self.edit_template.clear()
        self.clear_params_table()

    def on_delete(self):
        if not self.current:
            return
        pid = self.current.plugin_id
        if pid in self.registry.plugins:
            del self.registry.plugins[pid]
            self.registry.save()
            self.current = None
            self.refresh_list()
            self.on_new()

    def on_save(self):
        pid = self.edit_id.text().strip()
        if not pid:
            QtWidgets.QMessageBox.warning(self, "Missing ID", "Plugin ID is required")
            return

        params = self.collect_params_from_table()

        plugin = PluginDef(
            plugin_id=pid,
            name=self.edit_name.text().strip() or pid,
            category=self.edit_category.text().strip() or "General",
            perl_template=self.edit_template.toPlainText(),
            params=params
        )
        self.registry.plugins[pid] = plugin
        self.registry.save()
        self.current = plugin
        self.refresh_list()


# -------------------------
# Palette & Script Tree
# -------------------------

class BlockPalette(QtWidgets.QListWidget):
    """
    Left-hand "Flow" palette: double-click to add a new block of that type.
    These are control-flow / operators.
    """
    block_selected = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.populate()
        self.itemDoubleClicked.connect(self.on_double_click)

    def populate(self):
        items = [
            ("IF", BLOCK_IF),
            ("ELSIF", BLOCK_ELSIF),
            ("ELSE", BLOCK_ELSE),
            ("WHILE", BLOCK_WHILE),
            ("FOR", BLOCK_FOR),
            ("FOREACH", BLOCK_FOREACH),
            ("NEXT", BLOCK_NEXT),
            ("RETURN", BLOCK_RETURN),
            ("COMMENT", BLOCK_COMMENT),
            ("MY VAR", BLOCK_MY_VAR),
            ("OUR VAR", BLOCK_OUR_VAR),
            ("SET VAR", BLOCK_SET_VAR),
            ("ARRAY/HASH ASSIGN", BLOCK_ARRAY_ASSIGN),
            ("SET BUCKET", BLOCK_SET_BUCKET),
            ("GET BUCKET", BLOCK_GET_BUCKET),
            ("DELETE BUCKET", BLOCK_DELETE_BUCKET),
            ("SET TIMER (SECONDS)", BLOCK_TIMER),
            ("PLUGIN CALL", BLOCK_PLUGIN),
            ("QUEST:: CALL", BLOCK_QUEST_CALL),
            ("RAW PERL", BLOCK_RAW_PERL),
        ]

        # Alphabetize by display name
        items.sort(key=lambda x: x[0].lower())

        for text, block_type in items:
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, block_type)
            self.addItem(item)


    def on_double_click(self, item):
        block_type = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.block_selected.emit(block_type)

class DiagnosticsPanel(QtWidgets.QTabWidget):
    """
    Tabs:
      - Validation: list of issues
      - Live Perl: live generated Perl preview
      - Perl -c: on-demand perl syntax check output
    """
    run_perl_c_requested = QtCore.pyqtSignal()
    go_to_issue_requested = QtCore.pyqtSignal(object)  # emits ValidationIssue

    def __init__(self, parent=None):
        super().__init__(parent)

        # Validation tab
        self.validation_list = QtWidgets.QListWidget()
        self.validation_list.setWordWrap(True)
        self.addTab(self.validation_list, "Validation")
        self.validation_list.itemDoubleClicked.connect(self._on_validation_double_clicked)

        # Live Perl tab
        self.perl_preview = QtWidgets.QPlainTextEdit()
        self.perl_preview.setReadOnly(True)
        self.perl_preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.perl_preview.setFont(QtGui.QFont("Consolas", 9))
        self.addTab(self.perl_preview, "Live Perl")

        # Perl -c tab (on demand)
        perl_c_tab = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(perl_c_tab)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)

        top = QtWidgets.QHBoxLayout()
        self.perl_c_btn = QtWidgets.QPushButton("Run perl -c")
        self.perl_c_btn.setToolTip("Runs `perl -c` against the currently generated script.")
        self.perl_c_btn.clicked.connect(self.run_perl_c_requested.emit)
        top.addWidget(self.perl_c_btn)

        top.addStretch(1)

        self.perl_c_status = QtWidgets.QLabel("Not run yet.")
        self.perl_c_status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        top.addWidget(self.perl_c_status)

        v.addLayout(top)

        self.perl_c_output = QtWidgets.QPlainTextEdit()
        self.perl_c_output.setReadOnly(True)
        self.perl_c_output.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.perl_c_output.setFont(QtGui.QFont("Consolas", 9))
        v.addWidget(self.perl_c_output)

        self.addTab(perl_c_tab, "Perl -c")
    
    def set_perl_c_output_text(self, text: str):
        self.perl_c_output.setPlainText(text or "")

    def set_perl_c_status(self, ok: bool | None):
        # ok: True = green OK, False = red error, None = neutral
        if ok is True:
            self.perl_c_status.setText("<b style='color:#0f9d58;'>syntax OK</b>")
        elif ok is False:
            self.perl_c_status.setText("<b style='color:#ea4335;'>syntax errors</b>")
        else:
            self.perl_c_status.setText("Not run yet.")
    
    def _on_validation_double_clicked(self, item: QtWidgets.QListWidgetItem):
        issue = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if issue is not None:
            self.go_to_issue_requested.emit(issue)

    def set_validation(self, issues: list[ValidationIssue]):
        self.validation_list.clear()
        if not issues:
            self.validation_list.addItem("✅ No issues found.")
            return

        for it in issues:
            prefix = {"error": "❌", "warn": "⚠️", "info": "ℹ️"}.get(it.level, "•")
            where = f" [{it.where}]" if it.where else ""
            text = f"{prefix} {it.message}{where}"

            row = QtWidgets.QListWidgetItem(text)
            row.setData(QtCore.Qt.ItemDataRole.UserRole, it)  # <-- store object
            self.validation_list.addItem(row)

    def set_perl(self, perl_text: str):
        self.perl_preview.setPlainText(perl_text or "")

    def set_perl_check(self, output: str, ok: bool | None):
        self.perl_c_output.setPlainText(output or "")
        if ok is None:
            self.perl_c_status.setText("<i>Not run yet.</i>")
        elif ok:
            self.perl_c_status.setText("<b style='color:#0f9d58;'>syntax OK</b>")
        else:
            self.perl_c_status.setText("<b style='color:#ea4335;'>syntax errors</b>")

class BlockStyleDelegate(QtWidgets.QStyledItemDelegate):
    """
    Paint a subtle rounded-rect 'pill' behind each item so block types feel distinct.
    Uses item data to fetch a style key and then draws a background.
    """
    ROLE_STYLE_KEY = int(QtCore.Qt.ItemDataRole.UserRole) + 101

    def paint(self, painter, option, index):
        style_key = index.data(QtCore.Qt.ItemDataRole(self.ROLE_STYLE_KEY))

        # draw subtle pill *behind* the normal item paint
        if style_key:
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

            # Determine light vs dark from palette base
            base = option.palette.color(QtGui.QPalette.ColorRole.Base)
            is_dark = base.lightness() < 128

            # Muted group colors (NOT rainbow). These are "accent-ish" but softened via alpha.
            group_rgb = {
                "cond":  QtGui.QColor(90, 140, 210),   # conditionals
                "loop":  QtGui.QColor(110, 170, 120),  # loops
                "flow":  QtGui.QColor(200, 150, 90),   # next/return
                "act":   QtGui.QColor(170, 120, 200),  # plugin/quest/timer/method
                "data":  QtGui.QColor(120, 170, 190),  # vars/buckets/assign
                "meta":  QtGui.QColor(150, 150, 150),  # event/raw/comment
            }.get(str(style_key), QtGui.QColor(150, 150, 150))

            # Theme-aware subtlety
            c = QtGui.QColor(group_rgb)
            c.setAlpha(55 if is_dark else 30)  # darker theme needs a bit more alpha

            # Slightly inset so selection outline still looks nice
            r = option.rect.adjusted(3, 2, -3, -2)

            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QBrush(c))
            painter.drawRoundedRect(r, 6, 6)

            painter.restore()

        # Let Qt paint text/selection normally (keeps theme compatibility)
        super().paint(painter, option, index)

class ScriptTree(QtWidgets.QTreeWidget):
    
    # ...
    _ROLE_BASE_ICON = int(QtCore.Qt.ItemDataRole.UserRole) + 101
    _ROLE_BASE_BRUSH = int(QtCore.Qt.ItemDataRole.UserRole) + 102

    def _style_key_for(self, block_type: str) -> str:
        # Group block types into a few logical buckets (no rainbow).
        if block_type in (BLOCK_IF, BLOCK_ELSIF, BLOCK_ELSE):
            return "cond"
        if block_type in (BLOCK_WHILE, BLOCK_FOR, BLOCK_FOREACH):
            return "loop"
        if block_type in (BLOCK_NEXT, BLOCK_RETURN):
            return "flow"
        if block_type in (BLOCK_PLUGIN, BLOCK_QUEST_CALL, BLOCK_METHOD, BLOCK_TIMER):
            return "act"
        if block_type in (
            BLOCK_MY_VAR, BLOCK_OUR_VAR, BLOCK_SET_VAR, BLOCK_ARRAY_ASSIGN,
            BLOCK_SET_BUCKET, BLOCK_GET_BUCKET, BLOCK_DELETE_BUCKET
        ):
            return "data"
        if block_type in (BLOCK_EVENT, BLOCK_RAW_PERL, BLOCK_COMMENT):
            return "meta"
        return "meta"

    def _shape_prefix_for(self, block_type: str) -> str:
        # Human-readable type badges (high clarity, works even if icons change).
        return {
            BLOCK_EVENT:    "[EVENT] ",
            BLOCK_IF:       "[IF] ",
            BLOCK_ELSIF:    "[ELSIF] ",
            BLOCK_ELSE:     "[ELSE] ",
            BLOCK_WHILE:    "[WHILE] ",
            BLOCK_FOR:      "[FOR] ",
            BLOCK_FOREACH:  "[FOREACH] ",
            BLOCK_TIMER:    "[TIMER] ",
            BLOCK_PLUGIN:   "[PLUGIN] ",
            BLOCK_QUEST_CALL:"[QUEST] ",
            BLOCK_METHOD:   "[METHOD] ",
            BLOCK_RAW_PERL: "[RAW] ",
            BLOCK_NEXT:     "[NEXT] ",
            BLOCK_RETURN:   "[RETURN] ",
            BLOCK_COMMENT:  "[COMMENT] ",
            BLOCK_MY_VAR:   "[MY] ",
            BLOCK_OUR_VAR:  "[OUR] ",
            BLOCK_SET_VAR:  "[SET] ",
            BLOCK_ARRAY_ASSIGN: "[ASSIGN] ",
            BLOCK_SET_BUCKET:   "[SET_BUCKET] ",
            BLOCK_GET_BUCKET:   "[GET_BUCKET] ",
            BLOCK_DELETE_BUCKET:"[DEL_BUCKET] ",
        }.get(block_type, "[BLOCK] ")

    def _restore_base_visuals_only(self, item: QtWidgets.QTreeWidgetItem):
        """
        Restore the item's base icon after issue markers (warn/error) were applied.
        Do NOT restore cached foreground colors (we want palette-based text that works
        in both light/dark themes).
        """
        base_icon = item.data(0, QtCore.Qt.ItemDataRole(self._ROLE_BASE_ICON))
        if isinstance(base_icon, QtGui.QIcon):
            item.setIcon(0, base_icon)

        # IMPORTANT: Do not reset foreground from cached brushes.
        # Let Qt palette handle text color for theme switching.

    def _base_icon_for(self, block_type: str) -> QtGui.QIcon:
        st = self.style()
        return {
            BLOCK_EVENT: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ArrowRight),
            BLOCK_IF: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView),
            BLOCK_ELSIF: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView),
            BLOCK_ELSE: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView),
            BLOCK_WHILE: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_BrowserReload),
            BLOCK_FOR: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_BrowserReload),
            BLOCK_FOREACH: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_BrowserReload),
            BLOCK_PLUGIN: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay),
            BLOCK_QUEST_CALL: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay),
            BLOCK_METHOD: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay),
            BLOCK_RAW_PERL: st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon),
        }.get(block_type, st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon))

    def _apply_base_visuals(self, item: QtWidgets.QTreeWidgetItem, block: "Block") -> None:
        # Text (shape + label)
        item.setText(0, f"{self._shape_prefix_for(block.type)}{block.label}")

        # Icon
        base_icon = self._base_icon_for(block.type)
        item.setData(0, QtCore.Qt.ItemDataRole(self._ROLE_BASE_ICON), base_icon)
        item.setIcon(0, base_icon)

        # Font emphasis
        f = item.font(0)
        f.setBold(block.type in (BLOCK_EVENT,))
        item.setFont(0, f)

        # Base foreground (store so issue markers can override safely)
        item.setData(
            0,
            QtCore.Qt.ItemDataRole(BlockStyleDelegate.ROLE_STYLE_KEY),
            self._style_key_for(block.type),
        )
        
    def _apply_base_visual_style(self, item: QtWidgets.QTreeWidgetItem, block: "Block"):
        """
        Back-compat wrapper used by load/insert paths.
        Applies readable text + icon + theme-safe foreground.
        """
        self._apply_base_visuals(item, block)

    """
    Center: tree of blocks representing the script.
    Uses item.data(UserRole) to store/retrieve the Block.
    """
    selection_changed = QtCore.pyqtSignal(object)  # Block or None
    structure_changed = QtCore.pyqtSignal()

    def insert_block_object(self, block: Block, parent_item=None):
        item = QtWidgets.QTreeWidgetItem([block.label])
        item.setFlags(
            item.flags()
            | QtCore.Qt.ItemFlag.ItemIsDragEnabled
            | QtCore.Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, block)
        self._apply_base_visual_style(item, block)

        if parent_item is None:
            self.addTopLevelItem(item)
        else:
            parent_item.addChild(item)
            parent_item.setExpanded(True)

        for child in block.children:
            self.insert_block_object(child, item)

        self.setCurrentItem(item)
        return item

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(BlockStyleDelegate(self))
        self.setHeaderHidden(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)

        # Enable drag & drop
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

        # Keep selection updates wired
        self.itemSelectionChanged.connect(self.on_selection_changed)

        # Watch underlying model for structural changes (insert/move/remove)
        model = self.model()
        model.rowsInserted.connect(self._on_model_rows_changed)
        model.rowsRemoved.connect(self._on_model_rows_changed)
        # Internal moves fire rowsMoved, not inserted/removed
        if hasattr(model, "rowsMoved"):
            model.rowsMoved.connect(self._on_model_rows_moved)

    def select_block(self, block: Block) -> bool:
        if block is None:
            return False

        def find(item: QtWidgets.QTreeWidgetItem):
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole) is block:
                return item
            for i in range(item.childCount()):
                hit = find(item.child(i))
                if hit:
                    return hit
            return None

        for i in range(self.topLevelItemCount()):
            hit = find(self.topLevelItem(i))
            if hit:
                self.setCurrentItem(hit)
                self.scrollToItem(hit, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
                return True

        return False

    # ---------- helpers ----------

    def clear_issue_markers(self):
        def visit(item: QtWidgets.QTreeWidgetItem):
            self._restore_base_visuals_only(item)
            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.topLevelItemCount()):
            visit(self.topLevelItem(i))

    def apply_issue_markers(self, issues: list[ValidationIssue]):
        # block -> highest severity
        severity_by_block_id = {}

        for it in issues:
            b = getattr(it, "block_ref", None)
            if not b:
                continue

            bid = id(b)
            sev = it.level
            prev = severity_by_block_id.get(bid)
            if prev is None or SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(prev, 0):
                severity_by_block_id[bid] = sev


        def visit(item: QtWidgets.QTreeWidgetItem):
            block = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            sev = severity_by_block_id.get(id(block)) if block else None


            if sev == "error":
                item.setIcon(
                    0,
                    self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxCritical)
                )
                item.setForeground(0, QtGui.QBrush(QtGui.QColor("#ea4335")))
            elif sev == "warn":
                item.setIcon(
                    0,
                    self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning)
                )
                item.setForeground(0, QtGui.QBrush(QtGui.QColor("#fbbc04")))

            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.topLevelItemCount()):
            visit(self.topLevelItem(i))
            
    def _emit_structure_changed(self):
        """
        Emit structure_changed *after* the model is fully updated.
        Using singleShot(0, ...) keeps us safely after the drop/move.
        """
        QtCore.QTimer.singleShot(0, self.structure_changed.emit)

    # ---------- model change handlers ----------

    def _on_model_rows_changed(self, *args):
        # rowsInserted / rowsRemoved
        self._emit_structure_changed()

    def _on_model_rows_moved(self, *args):
        # Internal row moves
        self._emit_structure_changed()

    # ---------- drag & drop ----------

    def dropEvent(self, event: QtGui.QDropEvent):
        # Let the built-in behavior actually move the items
        super().dropEvent(event)

        # Re-emit selection changed for the new item position
        item = self.currentItem()
        block = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None
        self.selection_changed.emit(block)

        # Treat this as a structural change so undo/redo sees it
        self._emit_structure_changed()

    # ---------- existing API ----------

    def clear_script(self):
        self.clear()

    def add_block(self, block_type: str, parent_item=None):
        """
        Create a Block + tree item and insert it.
        Returns (block, item).
        """
        label = self._default_label(block_type)
        block = Block(type=block_type, label=label, params={}, children=[])

        item = QtWidgets.QTreeWidgetItem([label])
        item.setFlags(
            item.flags()
            | QtCore.Qt.ItemFlag.ItemIsDragEnabled
            | QtCore.Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, block)

        if parent_item is None:
            self.addTopLevelItem(item)
        else:
            parent_item.addChild(item)
            parent_item.setExpanded(True)

        self.setCurrentItem(item)
        return block, item

    def _default_label(self, block_type: str) -> str:
        if block_type == BLOCK_EVENT:
            return "EVENT_SAY"
        if block_type == BLOCK_IF:
            return "if ( ... )"
        if block_type == BLOCK_ELSIF:
            return "elsif ( ... )"
        if block_type == BLOCK_ELSE:
            return "else"
        if block_type == BLOCK_WHILE:
            return "while ( ... )"
        if block_type == BLOCK_FOR:
            return "for (...)"
        if block_type == BLOCK_FOREACH:
            return "foreach my $x (@list)"
        if block_type == BLOCK_NEXT:
            return "next"
        if block_type == BLOCK_RETURN:
            return "return"
        if block_type == BLOCK_COMMENT:
            return "# comment"
        if block_type == BLOCK_MY_VAR:
            return "my $var = value"
        if block_type == BLOCK_OUR_VAR:
            return "our $var = value"
        if block_type == BLOCK_SET_VAR:
            return "Set $var = value"
        if block_type == BLOCK_ARRAY_ASSIGN:
            return "$hash{$key} = value"
        if block_type == BLOCK_SET_BUCKET:
            return "Set bucket"
        if block_type == BLOCK_GET_BUCKET:
            return "Get bucket"
        if block_type == BLOCK_DELETE_BUCKET:
            return "Delete bucket"
        if block_type == BLOCK_TIMER:
            return "Set timer"
        if block_type == BLOCK_PLUGIN:
            return "Plugin call"
        if block_type == BLOCK_QUEST_CALL:
            return "quest::say(...)"
        if block_type == BLOCK_RAW_PERL:
            return "Raw Perl"
        if block_type == BLOCK_METHOD:
            return "Method call"
        return block_type

    def current_block(self) -> Optional[Block]:
        item = self.currentItem()
        if not item:
            return None
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole)

    def rebuild_block_tree(self) -> List[Block]:
        """
        Reconstruct block hierarchy from QTreeWidget items.
        """
        def build_from_item(item: QtWidgets.QTreeWidgetItem) -> Block:
            block: Block = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
            block.children = []
            for i in range(item.childCount()):
                child_item = item.child(i)
                child_block = build_from_item(child_item)
                block.children.append(child_block)
            return block

        result = []
        for i in range(self.topLevelItemCount()):
            top_item = self.topLevelItem(i)
            result.append(build_from_item(top_item))
        return result

    def load_from_blocks(self, blocks: List[Block]):
        """
        Clear the tree and rebuild it from a list of Block objects.
        """
        self.clear_script()

        def add_recursive(block: Block, parent_item: Optional[QtWidgets.QTreeWidgetItem]):
            item = QtWidgets.QTreeWidgetItem([block.label])
            item.setFlags(
                item.flags()
                | QtCore.Qt.ItemFlag.ItemIsDragEnabled
                | QtCore.Qt.ItemFlag.ItemIsDropEnabled
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, block)
            self._apply_base_visual_style(item, block)

            if parent_item is None:
                self.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
                parent_item.setExpanded(True)

            for child in block.children:
                add_recursive(child, item)

        for b in blocks:
            add_recursive(b, None)

    def on_selection_changed(self):
        item = self.currentItem()
        block = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None
        self.selection_changed.emit(block)

    def update_item_label(self, block: "Block"):
        # Update all items that match this block reference (text + icon + shape)
        def recurse(parent: Optional[QtWidgets.QTreeWidgetItem]):
            if parent is None:
                for i in range(self.topLevelItemCount()):
                    recurse(self.topLevelItem(i))
                return

            if parent.data(0, QtCore.Qt.ItemDataRole.UserRole) is block:
                self._apply_base_visuals(parent, block)

            for i in range(parent.childCount()):
                recurse(parent.child(i))

        recurse(None)

    def delete_current(self):
        """
        Delete the currently selected block (tree item).
        """
        item = self.currentItem()
        if not item:
            return

        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        else:
            idx = self.indexOfTopLevelItem(item)
            if idx >= 0:
                self.takeTopLevelItem(idx)

        self.selection_changed.emit(None)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        item = self.itemAt(event.pos())
        menu = QtWidgets.QMenu(self)

        act_add_if = menu.addAction("Add IF child")
        act_add_raw = menu.addAction("Add Raw Perl child")
        menu.addSeparator()
        act_delete = menu.addAction("Delete block")

        action = menu.exec(self.mapToGlobal(event.pos()))
        if not action:
            return

        changed = False

        if action == act_add_if:
            self.add_block(BLOCK_IF, parent_item=item)
            changed = True
        elif action == act_add_raw:
            self.add_block(BLOCK_RAW_PERL, parent_item=item)
            changed = True
        elif action == act_delete:
            self.delete_current()
            changed = True

        if changed:
            self._emit_structure_changed()

# -------------------------
# Block Property Editor
# -------------------------

class BlockPropertyEditor(QtWidgets.QWidget):
    """
    Right: show/edit parameters for the selected block.
    Cleaner layout with form-style rows, multi-line editors,
    and a help footer per block.
    """
    block_changed = QtCore.pyqtSignal(object)  # Block
    timer_defined = QtCore.pyqtSignal(str)
    
    def __init__(self, plugin_registry: PluginRegistry, parent=None):
        super().__init__(parent)
        self.registry = plugin_registry
        self.block: Optional[Block] = None

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)

        self.inner = QtWidgets.QWidget()
        self.form = QtWidgets.QFormLayout(self.inner)
        self.form.setContentsMargins(8, 6, 8, 6)
        self.form.setSpacing(6)
        self.form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self.form.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )

        self.scroll.setWidget(self.inner)
        self.clear_form()

    # ---------- helpers ----------
    
    def _make_header(self, text: str) -> None:
        lbl = QtWidgets.QLabel(f"<b>{text}</b>")
        lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        self.form.addRow(lbl)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        self.form.addRow(line)

    def _make_code_editor(self, text: str, min_height: int = 80) -> QtWidgets.QPlainTextEdit:
        edit = QtWidgets.QPlainTextEdit(text)
        edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        edit.setTabChangesFocus(False)
        edit.setMinimumHeight(min_height)
        font = QtGui.QFont("Consolas", 9)
        edit.setFont(font)
        return edit

    def add_labeled_widget(self, title: str, widget: QtWidgets.QWidget):
        label = QtWidgets.QLabel(title)
        label.setWordWrap(True)
        self.form.addRow(label, widget)

    def _set_footer(self, text: str):
        if not text:
            return
        footer = QtWidgets.QLabel(text)
        footer.setWordWrap(True)
        footer.setStyleSheet(
            "color: #9aa0a6; font-size: 11px; margin-top: 8px; margin-bottom: 2px;"
        )
        self.form.addRow(footer)

    def clear_form(self):
        while self.form.count():
            item = self.form.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        placeholder = QtWidgets.QLabel("Select a block in the script to edit its properties.")
        placeholder.setWordWrap(True)
        self.form.addRow(placeholder)

    # ---------- public API ----------

    def set_block(self, block: Optional[Block]):
        self.block = block
        self.clear_form()
        if not block:
            return

        # Header
        self._make_header(block.type)

        # Label / title
        edit_label = QtWidgets.QLineEdit(block.label)
        self.add_labeled_widget("Display label", edit_label)

        def on_label_changed(text: str):
            block.label = text
            self.block_changed.emit(block)

        edit_label.textChanged.connect(on_label_changed)

        # Per-block UI
        if block.type in (BLOCK_EVENT,):
            self._build_event_form(block)
            footer = "EVENT blocks become Perl subroutines (sub EVENT_*). Child blocks run when that event fires in EQEmu."
        elif block.type in (BLOCK_IF, BLOCK_ELSIF):
            self._build_if_form(block)
            footer = "Condition is raw Perl (you can use $client, $npc, $text, etc.). Child blocks only run when this expression is true."
        elif block.type == BLOCK_WHILE:
            self._build_while_form(block)
            footer = "Simple while-loop. Be careful not to create infinite loops; they will lock the quest script."
        elif block.type == BLOCK_SET_VAR:
            self._build_set_var_form(block)
            footer = "Assign a Perl variable. Value is a Perl expression, not a literal string unless quoted."
        elif block.type == BLOCK_ARRAY_ASSIGN:
            self._build_array_assign_form(block)
            footer = "Assign into a hash/array slot, e.g. $hash{$key} or $array[$i]."
        elif block.type == BLOCK_MY_VAR:
            self._build_my_var_form(block)
            footer = "Declares a lexical variable (my). Good for local state inside an EVENT or control block."
        elif block.type == BLOCK_OUR_VAR:
            self._build_our_var_form(block)
            footer = "Declares a package-level variable (our). Use sparingly for globals shared across subs."
        elif block.type == BLOCK_NEXT:
            self._build_next_form(block)
            footer = "Skips to the next iteration of the nearest loop. Optional suffix like 'unless $cond' is appended verbatim."
        elif block.type == BLOCK_SET_BUCKET:
            self._build_set_bucket_form(block)
            footer = 'Stores a string value into an EQEmu NPC bucket (quest::set_data style). Key is global within the NPC scope.'
        elif block.type == BLOCK_GET_BUCKET:
            self._build_get_bucket_form(block)
            footer = "Reads a bucket into a variable. You are responsible for casting / parsing the string value."
        elif block.type == BLOCK_DELETE_BUCKET:
            self._build_delete_bucket_form(block)
            footer = "Deletes the named bucket key, if it exists."
        elif block.type == BLOCK_TIMER:
            self._build_timer_form(block)
            footer = "Convenience wrapper for quest::settimer(name, seconds). Use matching EVENT_TIMER blocks in the script."
        elif block.type == BLOCK_FOR:
            self._build_for_form(block)
            footer = "Classic C-style for loop. Good for counting or iterating explicit ranges."
        elif block.type == BLOCK_FOREACH:
            self._build_foreach_form(block)
            footer = "foreach over a list or keys of a hash. List expression can be multi-line Perl if needed."
        elif block.type == BLOCK_PLUGIN:
            self._build_plugin_form(block)
            footer = "Renders a call into one of your JSON-described plugins. Parameters are filled into the Perl template."
        elif block.type == BLOCK_QUEST_CALL:
            self._build_quest_form(block)
            footer = "Direct quest:: call. Arguments are raw Perl (strings must be quoted). Multi-line is allowed."
        elif block.type == BLOCK_RETURN:
            self._build_return_form(block)
            footer = "Return from the current subroutine. Optional value is inserted verbatim."
        elif block.type == BLOCK_COMMENT:
            self._build_comment_form(block)
            footer = "Pure comments. Multi-line text is emitted as # lines in the generated Perl."
        elif block.type == BLOCK_RAW_PERL:
            self._build_raw_perl_form(block)
            footer = "Raw Perl lines copied directly into the output. Use this when the block-based model is too limiting."
        elif block.type == BLOCK_METHOD:
            self._build_method_form(block)
            footer = "Builds $target->method(args). Arguments can be multi-line Perl for complex calls."
        elif block.type == BLOCK_ELSE:
            # ELSE has no condition; the body is defined by nested child blocks.
            self._build_else_form(block)
            footer = "ELSE has no condition. Add blocks *under* ELSE in the Script Tree to fill the { ... } body (nesting)."
        else:
            footer = ""

        self._set_footer(footer)

    # ---------- specific forms ----------

    def _build_event_form(self, block: Block):
        event_type_box = QtWidgets.QComboBox()
        event_type_box.addItems(NPC_EVENTS)
        event_type_box.setCurrentText(block.params.get("event_name", "EVENT_SAY"))
        self.add_labeled_widget("Event name", event_type_box)

        def on_event_changed(text: str):
            block.params["event_name"] = text
            block.label = text
            self.block_changed.emit(block)

        event_type_box.currentTextChanged.connect(on_event_changed)

    def _build_if_form(self, block: Block):
        expr_edit = self._make_code_editor(
            block.params.get("expr", '$text =~ /hail/i'),
            min_height=80,
        )
        self.add_labeled_widget("Condition (Perl expression)", expr_edit)

        def on_expr_change():
            text = expr_edit.toPlainText().strip()
            block.params["expr"] = text
            prefix = "if" if block.type == BLOCK_IF else "elsif"
            block.label = f"{prefix} ({text})" if text else f"{prefix} (...)"
            self.block_changed.emit(block)

        expr_edit.textChanged.connect(on_expr_change)

    def _build_while_form(self, block: Block):
        expr_edit = self._make_code_editor(
            block.params.get("expr", "1"),
            min_height=60,
        )
        self.add_labeled_widget("Loop condition (Perl)", expr_edit)

        def on_expr_change():
            text = expr_edit.toPlainText().strip()
            block.params["expr"] = text
            block.label = f"while ({text})" if text else "while (...)"
            self.block_changed.emit(block)

        expr_edit.textChanged.connect(on_expr_change)

    def _build_set_var_form(self, block: Block):
        name_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$myvar"))
        value_edit = self._make_code_editor(block.params.get("value", "0"), min_height=60)

        self.add_labeled_widget("Variable name", name_edit)
        self.add_labeled_widget("Value (Perl expression)", value_edit)

        def update():
            block.params["var_name"] = name_edit.text()
            value = value_edit.toPlainText()
            block.params["value"] = value
            block.label = f"Set {name_edit.text()} = {value}"
            self.block_changed.emit(block)

        name_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(update)

    def _build_array_assign_form(self, block: Block):
        lhs_edit = QtWidgets.QLineEdit(block.params.get("lhs", "$hash{$key}"))
        value_edit = self._make_code_editor(block.params.get("value", "0"), min_height=60)

        self.add_labeled_widget("LHS (e.g. $hash{$key}, $array[$i])", lhs_edit)
        self.add_labeled_widget("Value (Perl expression)", value_edit)

        def update():
            lhs = lhs_edit.text()
            val = value_edit.toPlainText()
            block.params["lhs"] = lhs
            block.params["value"] = val
            block.label = f"{lhs} = {val}"
            self.block_changed.emit(block)

        lhs_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(update)

    def _build_my_var_form(self, block: Block):
        name_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$myvar"))
        value_edit = self._make_code_editor(block.params.get("value", ""), min_height=80)

        self.add_labeled_widget("Variable name (my $x, my @arr)", name_edit)
        self.add_labeled_widget("Value (optional, multi-line OK)", value_edit)

        def update():
            block.params["var_name"] = name_edit.text()
            val = value_edit.toPlainText()
            if val.strip():
                block.params["value"] = val
                block.label = f"my {name_edit.text()} = {val}"
            else:
                block.params.pop("value", None)
                block.label = f"my {name_edit.text()}"
            self.block_changed.emit(block)

        name_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(update)

    def _build_our_var_form(self, block: Block):
        name_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$OurVar"))
        value_edit = self._make_code_editor(block.params.get("value", ""), min_height=80)

        self.add_labeled_widget("Variable name (our $Var)", name_edit)
        self.add_labeled_widget("Value (optional, multi-line OK)", value_edit)

        def update():
            block.params["var_name"] = name_edit.text()
            val = value_edit.toPlainText()
            if val.strip():
                block.params["value"] = val
                block.label = f"our {name_edit.text()} = {val}"
            else:
                block.params.pop("value", None)
                block.label = f"our {name_edit.text()}"
            self.block_changed.emit(block)

        name_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(update)

    def _build_next_form(self, block: Block):
        expr_edit = QtWidgets.QLineEdit(block.params.get("expr", ""))

        self.add_labeled_widget(
            "Condition suffix (e.g. 'unless $cond' or 'if $cond')",
            expr_edit,
        )

        def on_change(text: str):
            expr = text.strip()
            block.params["expr"] = expr
            label = "next"
            if expr:
                label += f" {expr}"
            block.label = label
            self.block_changed.emit(block)

        expr_edit.textChanged.connect(on_change)

    def _build_set_bucket_form(self, block: Block):
        key_edit = QtWidgets.QLineEdit(block.params.get("key", '$bucketkey'))
        value_edit = QtWidgets.QLineEdit(block.params.get("value", '""'))

        self.add_labeled_widget("Bucket key (Perl expr, e.g. $bucketkey or \"mykey\")", key_edit)
        self.add_labeled_widget("Value (Perl expr, e.g. $v or \"text\")", value_edit)

        def update():
            block.params["key"] = key_edit.text().strip()
            block.params["value"] = value_edit.text().strip()
            block.label = f"quest::set_data({block.params['key']}, ...)"
            self.block_changed.emit(block)

        key_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(lambda _: update())

        def update():
            block.params["key"] = key_edit.text()
            block.params["value"] = value_edit.text()
            block.label = f'Set bucket "{key_edit.text()}"'
            self.block_changed.emit(block)

        key_edit.textChanged.connect(lambda _: update())
        value_edit.textChanged.connect(lambda _: update())

    def _build_get_bucket_form(self, block: Block):
        key_edit = QtWidgets.QLineEdit(block.params.get("key", '$bucketkey'))
        var_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$value"))

        self.add_labeled_widget("Bucket key (Perl expr, e.g. $bucketkey or \"mykey\")", key_edit)
        self.add_labeled_widget("Store result in variable", var_edit)

        def update():
            block.params["key"] = key_edit.text().strip()
            block.params["var_name"] = var_edit.text().strip()
            block.label = f"{block.params['var_name']} = quest::get_data({block.params['key']})"
            self.block_changed.emit(block)

        key_edit.textChanged.connect(lambda _: update())
        var_edit.textChanged.connect(lambda _: update())

    def _build_delete_bucket_form(self, block: Block):
        key_edit = QtWidgets.QLineEdit(block.params.get("key", '$bucketkey'))
        self.add_labeled_widget("Bucket key (Perl expr, e.g. $bucketkey or \"mykey\")", key_edit)

        def update():
            block.params["key"] = key_edit.text().strip()
            block.label = f"quest::delete_data({block.params['key']})"
            self.block_changed.emit(block)

        key_edit.textChanged.connect(lambda _: update())

    def _build_timer_form(self, block: Block):
        name_edit = QtWidgets.QLineEdit(block.params.get("name", "my_timer"))
        sec_edit = QtWidgets.QSpinBox()
        sec_edit.setRange(0, 1_000_000)
        sec_edit.setValue(int(block.params.get("seconds", 10)))

        self.add_labeled_widget("Timer name", name_edit)
        self.add_labeled_widget("Seconds", sec_edit)

        # Track last meaningful timer name on the block itself (for rename-in-place)
        block.params.setdefault("_last_timer_name", (block.params.get("name") or "").strip())

        def update():
            old_name = (block.params.get("name") or "").strip()
            new_name = name_edit.text().strip()

            block.params["name"] = new_name
            block.params["seconds"] = sec_edit.value()
            block.label = f'Set timer "{new_name}" to {sec_edit.value()}s'
            self.block_changed.emit(block)

            # Determine which EVENT_* we are inside so EVENT_TIMER is created after it
            prefer_after_event = ""
            w = self.window()
            if w and hasattr(w, "script_tree"):
                item = w.script_tree.currentItem()
                p = item
                while p is not None:
                    bb = p.data(0, QtCore.Qt.ItemDataRole.UserRole)
                    if bb and getattr(bb, "type", None) == BLOCK_EVENT:
                        prefer_after_event = (bb.params.get("event_name") or "").strip()
                        break
                    p = p.parent()

            # Only attempt helper if we have a window with the helper
            if w and hasattr(w, "ensure_event_timer_handler"):
                old_for_helper = None

                # If user is renaming directly, old_name captures the previous value
                if old_name and new_name and old_name != new_name:
                    old_for_helper = old_name

                # If they blank it out then type again, use the last known non-empty name
                elif (not old_name) and new_name:
                    last = (block.params.get("_last_timer_name") or "").strip()
                    if last and last != new_name:
                        old_for_helper = last

                w.ensure_event_timer_handler(
                    new_name,
                    old_name=old_for_helper,
                    prefer_after_event=prefer_after_event
                )

            # Update last non-empty timer name *correctly*
            if new_name:
                block.params["_last_timer_name"] = new_name

        name_edit.textChanged.connect(lambda _=None: update())
        sec_edit.valueChanged.connect(lambda _=None: update())

        # Run once so helper creates EVENT_TIMER + IF branch immediately for the default name
        update()

    def _build_for_form(self, block: Block):
        var_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$i"))

        start_val = block.params.get("start", 0)
        end_val = block.params.get("end", 10)
        cmp_op_val = block.params.get("cmp_op", "<=")
        inc_expr_val = block.params.get("inc_expr", "++")

        start_edit = QtWidgets.QLineEdit(str(start_val))
        end_edit = QtWidgets.QLineEdit(str(end_val))

        cmp_combo = QtWidgets.QComboBox()
        cmp_combo.addItems(["<", "<=", ">", ">="])
        idx = cmp_combo.findText(cmp_op_val)
        if idx >= 0:
            cmp_combo.setCurrentIndex(idx)

        inc_edit = QtWidgets.QLineEdit(inc_expr_val)

        self.add_labeled_widget("Loop variable", var_edit)
        self.add_labeled_widget("Start value (Perl expression or number)", start_edit)
        self.add_labeled_widget("End value (Perl expression or number)", end_edit)
        self.add_labeled_widget("Comparison operator", cmp_combo)
        self.add_labeled_widget("Increment expression (e.g. ++, += 2)", inc_edit)

        def update():
            old_name = (block.params.get("name") or "").strip()
            new_name = name_edit.text().strip()

            block.params["name"] = new_name
            block.params["seconds"] = sec_edit.value()
            block.label = f'Set timer "{new_name}" to {sec_edit.value()}s'
            self.block_changed.emit(block)

            # keep track of last known name so rename works without creating extra IF branches
            last_name = (block.params.get("name") or "").strip()

            # Timer auto-helper: rename in place + update stoptimer()
            w = self.window()
            if w and hasattr(w, "ensure_event_timer_handler"):
                w.ensure_event_timer_handler(old_for_helper, new_name)


    def _build_foreach_form(self, block: Block):
        var_edit = QtWidgets.QLineEdit(block.params.get("var_name", "$x"))
        list_edit = self._make_code_editor(
            block.params.get("list_expr", "@list"),
            min_height=60,
        )

        self.add_labeled_widget("Loop variable (e.g. $item)", var_edit)
        self.add_labeled_widget("List expression (e.g. @items or keys %hash)", list_edit)
        last_name = (block.params.get("name") or "").strip()

        def update():
            block.params["var_name"] = var_edit.text()
            block.params["list_expr"] = list_edit.toPlainText().strip()
            block.label = f"foreach my {var_edit.text()} ({block.params['list_expr']})"
            self.block_changed.emit(block)

        var_edit.textChanged.connect(lambda _: update())
        list_edit.textChanged.connect(update)


    def _build_return_form(self, block: Block):
        value_edit = self._make_code_editor(block.params.get("value", ""), min_height=60)
        self.add_labeled_widget("Return value (optional)", value_edit)

        def update():
            val = value_edit.toPlainText().strip()
            block.params["value"] = val
            block.label = f"return {val}" if val else "return"
            self.block_changed.emit(block)

        value_edit.textChanged.connect(update)

    def _build_comment_form(self, block: Block):
        text = block.params.get("text", "")
        edit = self._make_code_editor(text, min_height=80)
        self.add_labeled_widget("Comment text", edit)

        def on_change():
            t = edit.toPlainText()
            block.params["text"] = t
            first = (t.splitlines()[0] if t.splitlines() else "").strip()
            if len(first) > 40:
                first = first[:37] + "..."
            block.label = "# " + first if first else "# comment"
            self.block_changed.emit(block)

        edit.textChanged.connect(on_change)

    def _build_plugin_form(self, block: Block):
        plugins = self.registry.list_plugins()
        combo = QtWidgets.QComboBox()
        combo.addItem("-- Select plugin --", userData=None)
        for p in plugins:
            combo.addItem(f"{p.name} ({p.plugin_id})", userData=p.plugin_id)

        block.params.setdefault("plugin_params", {})
        current_pid = block.params.get("plugin_id")
        if current_pid:
            idx = combo.findData(current_pid)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        self.add_labeled_widget("Plugin", combo)

        # Group box that will hold parameter editors
        params_group = QtWidgets.QGroupBox("Plugin parameters")
        params_vbox = QtWidgets.QVBoxLayout(params_group)
        params_vbox.setContentsMargins(4, 4, 4, 4)
        params_vbox.setSpacing(6)
        self.form.addRow(params_group)

        def rebuild_params(pid: Optional[str]):
            # Clear old widgets
            while params_vbox.count():
                item = params_vbox.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()

            if not pid:
                return

            pdef = self.registry.get(pid)
            if not pdef:
                return

            plugin_params = block.params.setdefault("plugin_params", {})

            # First pass: build widgets
            body_edit = None
            rewards_edit = None

            for p in pdef.params:
                name = p["name"]
                label = p.get("label", name)
                default = p.get("default", "")
                val = plugin_params.get(name, default)

                # Label on top
                lbl = QtWidgets.QLabel(label)
                lbl.setWordWrap(True)
                params_vbox.addWidget(lbl)

                # Decide widget type
                if name in ("body_on_success", "rewards", "body"):
                    # multi-line code editor
                    w = self._make_code_editor(str(val), min_height=120)
                    if name == "body_on_success" or name == "body":
                        body_edit = w
                    if name == "rewards":
                        rewards_edit = w
                else:
                    w = QtWidgets.QLineEdit(str(val))

                params_vbox.addWidget(w)

                def make_cb(param_name=name, widget=w):
                    def cb():
                        if isinstance(widget, QtWidgets.QPlainTextEdit):
                            plugin_params[param_name] = widget.toPlainText()
                        else:
                            plugin_params[param_name] = widget.text()
                        self.block_changed.emit(block)
                    return cb

                if isinstance(w, QtWidgets.QPlainTextEdit):
                    w.textChanged.connect(make_cb())
                else:
                    w.textChanged.connect(lambda _t, cb=make_cb(): cb())

            # Optional: try to visually split body / rewards space "evenly"
            # by giving them a larger minimum height if both exist.
            if body_edit and rewards_edit:
                body_edit.setMinimumHeight(120)
                rewards_edit.setMinimumHeight(120)

        def on_plugin_changed(_text: str):
            pid = combo.currentData()
            block.params["plugin_id"] = pid
            plugin_params = {}
            if pid:
                pdef = self.registry.get(pid)
                if pdef:
                    for p in pdef.params:
                        plugin_params[p["name"]] = p.get("default", "")
            block.params["plugin_params"] = plugin_params
            block.label = f"Plugin: {combo.currentText()}"
            self.block_changed.emit(block)
            rebuild_params(pid)

        combo.currentTextChanged.connect(on_plugin_changed)
        rebuild_params(current_pid)




    def _build_raw_perl_form(self, block: Block):
        text = block.params.get("code", "# your perl here")
        edit = self._make_code_editor(text, min_height=150)
        self.add_labeled_widget("Raw Perl block", edit)

        def on_change():
            block.params["code"] = edit.toPlainText()
            self.block_changed.emit(block)

        edit.textChanged.connect(on_change)
    
    def _build_else_form(self, block: Block):
        info = QtWidgets.QLabel(
            "else { ... }\n\n"
            "This block has no condition.\n"
            "To add code inside the braces, nest blocks under ELSE in the Script Tree:\n"
            "• Select ELSE, then double-click a palette item to insert as a child\n"
            "• Or drag/drop blocks beneath ELSE\n"
        )
        info.setWordWrap(True)
        self.add_labeled_widget("Else body", info)

    def _build_quest_form(self, block: Block):
        quest_cmds = [
            "say", "emote", "shout", "ze", "we",
            "ding", "summonitem", "spawn2", "unique_spawn",
            "signalwith", "setglobal", "delglobal",
            "set_data", "delete_data",
            "settimer", "stoptimer",
            "movepc",
        ]

        combo = QtWidgets.QComboBox()
        combo.addItems(quest_cmds)
        combo.setCurrentText(block.params.get("quest_fn", "say"))
        self.add_labeled_widget("quest:: function", combo)

        args_edit = self._make_code_editor(
            block.params.get("args", '"Hello, world!"'),
            min_height=80,
        )
        self.add_labeled_widget("Arguments (Perl, multi-line OK)", args_edit)

        def update():
            fn = combo.currentText()
            args = args_edit.toPlainText()
            block.params["quest_fn"] = fn
            block.params["args"] = args
            block.label = f"quest::{fn}({args})"
            self.block_changed.emit(block)

        combo.currentTextChanged.connect(lambda _: update())
        args_edit.textChanged.connect(update)

    def _build_method_form(self, block: Block):
        target_edit = QtWidgets.QLineEdit(block.params.get("target", "$client"))
        method_edit = QtWidgets.QLineEdit(block.params.get("method", "Message"))
        args_edit = self._make_code_editor(block.params.get("args", ""), min_height=80)

        self.add_labeled_widget("Target (e.g. $client, $npc)", target_edit)
        self.add_labeled_widget("Method name", method_edit)
        self.add_labeled_widget("Arguments (Perl, multi-line OK)", args_edit)

        def update():
            block.params["target"] = target_edit.text()
            block.params["method"] = method_edit.text()
            block.params["args"] = args_edit.toPlainText()
            args = block.params["args"].strip()
            if args:
                block.label = f'{block.params["target"]}->{block.params["method"]}({args})'
            else:
                block.label = f'{block.params["target"]}->{block.params["method"]}()'
            self.block_changed.emit(block)

        target_edit.textChanged.connect(lambda _: update())
        method_edit.textChanged.connect(lambda _: update())
        args_edit.textChanged.connect(update)


# -------------------------
# Perl code parsing
# -------------------------

def parse_perl_to_blocks(perl_text: str,
                         registry: Optional[PluginRegistry] = None) -> List[Block]:
    """
    Very lightweight parser that understands a subset of the Perl we generate.
    """
    blocks: List[Block] = []
    stack: List[Block] = []

    def add_block(b: Block):
        if stack:
            stack[-1].children.append(b)
        else:
            blocks.append(b)
    
    # --- State for multi-line my/our declarations like:
    #     my @arr = (
    #         "foo",
    #         "bar",
    #     );
    multiline_decl_kind: Optional[str] = None   # "my" or "our"
    multiline_var_name: Optional[str] = None    # e.g. "@funny_buff_quotes"
    multiline_lines: List[str] = []             # full lines including indentation

    def flush_multiline_decl():
        nonlocal multiline_decl_kind, multiline_var_name, multiline_lines
        if not multiline_decl_kind or not multiline_var_name or not multiline_lines:
            multiline_decl_kind = None
            multiline_var_name = None
            multiline_lines = []
            return

        value_text = "\n".join(multiline_lines).rstrip()

        # Represent as a single RAW_PERL block so code is preserved exactly
        b = Block(
            type=BLOCK_RAW_PERL,
            label=f"Raw Perl ({multiline_decl_kind} {multiline_var_name} ...)",
            params={"code": value_text},
            children=[]
        )
        add_block(b)

        multiline_decl_kind = None
        multiline_var_name = None
        multiline_lines = []
    
    # --- State for capturing custom subs (non EVENT_*) as RAW_PERL
    custom_sub_name: Optional[str] = None
    custom_sub_lines: List[str] = []
    custom_sub_depth: int = 0

    def _strip_strings_and_comment(s: str) -> str:
        """
        Remove quoted strings and trailing comments so braces inside quotes/comments
        don't affect depth.
        (Lightweight heuristic; good enough for typical EQEmu scripts.)
        """
        # Drop trailing comment (simple heuristic)
        if "#" in s:
            # NOTE: this is not perfect around quoted '#', but acceptable for our use
            s = s.split("#", 1)[0]

        # Remove simple quoted strings
        s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
        s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)
        return s

    def _brace_delta(line: str) -> int:
        t = _strip_strings_and_comment(line)
        return t.count("{") - t.count("}")

    def flush_custom_sub():
        nonlocal custom_sub_name, custom_sub_lines, custom_sub_depth
        if not custom_sub_name or not custom_sub_lines:
            custom_sub_name = None
            custom_sub_lines = []
            custom_sub_depth = 0
            return

        value_text = "\n".join(custom_sub_lines).rstrip()
        b = Block(
            type=BLOCK_RAW_PERL,
            label=f"Raw Perl (sub {custom_sub_name})",
            params={"code": value_text},
            children=[]
        )
        add_block(b)

        custom_sub_name = None
        custom_sub_lines = []
        custom_sub_depth = 0

    # Prebuild regex patterns for plugins, if registry is given
    plugin_patterns = []
    if registry is not None:
        for p in registry.list_plugins():
            template = p.perl_template.strip()

            # Escape everything first
            pattern = re.escape(template)

            # Replace {param} placeholders with named capture groups
            for param in p.params:
                name = param["name"]

                # Original placeholder in the template
                placeholder = "{" + name + "}"

                # How that placeholder looks *after* re.escape has been applied
                placeholder_escaped = re.escape(placeholder)          # "\{returnitems\}"

                # How that escaped text looks inside the escaped template string
                # (we must match the literal backslashes too)
                placeholder_in_pattern = re.escape(placeholder_escaped)  # "\\\{returnitems\\\}"

                pattern = re.sub(
                    placeholder_in_pattern,
                    fr"(?P<{name}>.+?)",
                    pattern
                )


            # Allow optional whitespace in some common spots (you can extend this later)
            pattern = pattern.replace(r"\ ", r"\s*")

            try:
                regex = re.compile(r"^" + pattern + r"$")
            except re.error:
                # If something goes wrong, skip this plugin for parsing
                continue

            plugin_patterns.append((p, regex))

    comment_accum: List[str] = []

    def flush_comment():
        nonlocal comment_accum
        if not comment_accum:
            return
        text = "\n".join(comment_accum)
        first = comment_accum[0].strip()
        if len(first) > 40:
            first = first[:37] + "..."
        label = "# " + first if first else "# comment"
        b = Block(
            type=BLOCK_COMMENT,
            label=label,
            params={"text": text},
            children=[]
        )
        add_block(b)
        comment_accum = []

    lines = perl_text.splitlines()

    # Precompiled regexes
    re_sub_event = re.compile(r'^sub\s+(EVENT_\w+)\s*{')
    re_sub_any = re.compile(r'^sub\s+(\w+)\s*{')
    re_if        = re.compile(r'^if\s*\((.+)\)\s*{')
    re_elsif = re.compile(r'^\s*elsif\s*\((.+)\)\s*\{')
    re_else  = re.compile(r'^\s*else\s*\{')
    re_while     = re.compile(r'^while\s*\((.+)\)\s*{')
    re_foreach   = re.compile(r'^foreach\s+my\s+(\$\w+)\s*\((.+)\)\s*{$')
    re_for       = re.compile(
        r'^for\s*\(\s*my\s+(\$\w+)\s*=\s*([^;]+);\s*'   # init: my $i = START
        r'\1\s*(<=|<)\s*([^;]+);\s*'                    # cond:  $i < END  or $i <= END
        r'\1\s*(\+\+|\-\-|\+=\s*[^)]+|-=\s*[^)]+)\s*\)\s*{'  # inc: $i++, $i--, $i += step, $i -= step
    )
    re_quest     = re.compile(r'^quest::(\w+)\((.*)\);')
    re_settimer  = re.compile(r'^quest::settimer\(\s*"([^"]+)"\s*,\s*([0-9_]+)\s*\);')
    re_my  = re.compile(
        r'^my\s+([\$\@\%]\w+)\s*'          # var name: $foo, @list, %hash
        r'(?:=\s*(.+?))?'                  # optional RHS, non-greedy
        r';\s*(?:#.*)?$'                   # semicolon, optional spaces, optional comment
    )
    re_multiline_decl_start = re.compile(
        r'^(my|our)\s+([\$\@\%]\w+)\s*=\s*\(\s*$'
    )

    re_our = re.compile(
        r'^our\s+([\$\@\%]\w+)\s*'
        r'(?:=\s*(.+?))?'
        r';\s*(?:#.*)?$'
    )
    re_setvar    = re.compile(r'^(\$\w+)\s*=\s*(.+);')
    re_bucket_set    = re.compile(r'^\$npc->SetBucket\("([^"]+)"\s*,\s*"([^"]*)"\);')
    re_bucket_get    = re.compile(r'^(\$\w+)\s*=\s*\$npc->GetBucket\("([^"]+)"\);')
    re_bucket_delete = re.compile(r'^\$npc->DeleteBucket\("([^"]+)"\);')
    re_method    = re.compile(r'^(\$\w+)->(\w+)\((.*)\);')
    re_return    = re.compile(r'^return\b(.*);?')
    re_next      = re.compile(r'^next\b(.*);$')
    re_array_assign = re.compile(r'^(\$\w+\s*\{\s*[^}]+\s*\})\s*=\s*(.+);$')



    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
    # --- If we're capturing a custom sub, keep appending until braces balance
        if custom_sub_name is not None:
            custom_sub_lines.append(line)
            custom_sub_depth += _brace_delta(line)
            if custom_sub_depth <= 0:
                flush_custom_sub()
            continue

        # --- If we're in the middle of a multi-line declaration, keep accumulating
        if multiline_decl_kind is not None:
            multiline_lines.append(line)
            # End when we hit a line ending with ');'
            if stripped.endswith(");"):
                flush_multiline_decl()
            # Either way, skip the rest of the parsing for this line
            continue
            
        if not stripped:
            flush_comment()
            continue

        # Comments
        if stripped.startswith("#"):
            comment_accum.append(stripped.lstrip("#").strip())
            continue
        else:
            flush_comment()

        # Closing brace ends the current block scope
        if stripped.startswith("}"):
            if stack:
                stack.pop()
            continue

        # --- sub EVENT_* { ... } ---
        # --- Start of a multi-line my/our declaration?
        m = re_multiline_decl_start.match(stripped)
        if m:
            # Flush any pending comments before we start the declaration
            flush_comment()
            multiline_decl_kind = m.group(1)      # "my" or "our"
            multiline_var_name = m.group(2)       # e.g. "@funny_buff_quotes"
            multiline_lines = [line]              # include this header line
            # If someone wrote everything on one line (rare), we can still end it
            if stripped.endswith(");"):
                flush_multiline_decl()
            continue
        
            # --- next; / next unless ...; / next if ...; ---
        m = re_sub_event.match(stripped)
        if m:
            event_name = m.group(1).strip()

            b = Block(
                type=BLOCK_EVENT,
                label=event_name,
                params={"event_name": event_name},
                children=[]
            )
            add_block(b)
            stack.append(b)
            continue

        # --- sub SomeHelper { ... } (non-EVENT) ---
        m = re_sub_any.match(stripped)
        if m:
            sub_name = m.group(1).strip()
            if not sub_name.startswith("EVENT_"):
                flush_comment()
                custom_sub_name = sub_name
                custom_sub_lines = [line]
                custom_sub_depth = _brace_delta(line)  # '{' on this line already counts
                # If sub opens and closes on same line (rare), flush immediately
                if custom_sub_depth <= 0:
                    flush_custom_sub()
                continue

        m = re_next.match(stripped)
        if m:
            suffix = m.group(1).strip()  # may be '', 'unless $cond', 'if $cond', etc.
            label = "next"
            if suffix:
                label += f" {suffix}"
            b = Block(
                type=BLOCK_NEXT,
                label=label,
                params={"expr": suffix},
                children=[]
            )
            add_block(b)
            continue

        # --- if / elsif / else / while / foreach / for ---
        m = re_if.match(stripped)
        if m:
            expr = m.group(1).strip()
            b = Block(
                type=BLOCK_IF,
                label=f"if ({expr})",
                params={"expr": expr},
                children=[]
            )
            add_block(b)
            stack.append(b)
            continue

        m = re_elsif.match(stripped)
        if m:
            expr = m.group(1).strip()

            # ELSIF closes previous IF / ELSIF
            if stack and stack[-1].type in (BLOCK_IF, BLOCK_ELSIF):
                stack.pop()

            b = Block(
                type=BLOCK_ELSIF,
                label=f"elsif ({expr})",
                params={"expr": expr},
                children=[]
            )
            add_block(b)      # sibling, not child
            stack.append(b)
            continue

        if re_else.match(stripped):

            # ELSE closes previous IF / ELSIF
            if stack and stack[-1].type in (BLOCK_IF, BLOCK_ELSIF):
                stack.pop()

            b = Block(
                type=BLOCK_ELSE,
                label="else",
                params={},
                children=[]
            )
            add_block(b)      # sibling, not child
            stack.append(b)
            continue

        m = re_while.match(stripped)
        if m:
            expr = m.group(1).strip()
            b = Block(
                type=BLOCK_WHILE,
                label=f"while ({expr})",
                params={"expr": expr},
                children=[]
            )
            add_block(b)
            stack.append(b)
            continue

        m = re_foreach.match(stripped)
        if m:
            var_name, list_expr = m.groups()
            var_name = var_name.strip()
            list_expr = list_expr.strip()
            b = Block(
                type=BLOCK_FOREACH,
                label=f"foreach my {var_name} ({list_expr})",
                params={"var_name": var_name, "list_expr": list_expr},
                children=[]
            )
            add_block(b)
            stack.append(b)
            continue

        m = re_for.match(stripped)
        if m:
            var_name, start, cmp_op, end_expr, inc_expr = m.groups()
            start = start.strip()
            end_expr = end_expr.strip()
            cmp_op = cmp_op.strip()
            inc_expr = inc_expr.strip()  # "++", "--", "+= 2", etc.

            # Try to turn the numeric-looking pieces into ints, leave expressions as-is
            def _to_int_or_str(s: str):
                s_clean = s.replace("_", "").strip()
                try:
                    return int(s_clean)
                except ValueError:
                    return s.strip()

            start_v = _to_int_or_str(start)
            end_v   = _to_int_or_str(end_expr)

            b = Block(
                type=BLOCK_FOR,
                label=f"for ({var_name}={start}..{end_expr} {cmp_op} {inc_expr})",
                params={
                    "var_name": var_name,
                    "start": start_v,
                    "end": end_v,
                    "cmp_op": cmp_op,     # "<" or "<="
                    "inc_expr": inc_expr  # "++", "+= 2", etc.
                },
                children=[]
            )
            add_block(b)
            stack.append(b)
            continue

        # --- quest::settimer -> TIMER block ---
        m = re_settimer.match(stripped)
        if m:
            name, sec = m.groups()
            sec_val = int(sec.replace("_", "")) if sec.replace("_", "").isdigit() else 10
            b = Block(
                type=BLOCK_TIMER,
                label=f'Set timer "{name}" to {sec_val}s',
                params={"name": name, "seconds": sec_val},
                children=[]
            )
            add_block(b)
            continue

        # --- quest::<fn>(...) ---
        m = re_quest.match(stripped)
        if m:
            fn, args = m.groups()
            fn = fn.strip()
            args = args.strip()
            b = Block(
                type=BLOCK_QUEST_CALL,
                label=f"quest::{fn}({args})",
                params={"quest_fn": fn, "args": args},
                children=[]
            )
            add_block(b)
            continue

        # --- my / our / var assignment ---
        m = re_my.match(stripped)
        if m:
            var_name = m.group(1)
            value = (m.group(2) or "").strip()  # optional RHS

            params = {"var_name": var_name}
            if value:
                params["value"] = value

            label = f"my {var_name}"
            if value:
                label += f" = {value}"

            b = Block(
                type=BLOCK_MY_VAR,
                label=label,
                params=params,
                children=[]
            )
            add_block(b)
            continue

        m = re_our.match(stripped)
        if m:
            var_name = m.group(1)
            value = (m.group(2) or "").strip()

            params = {"var_name": var_name}
            if value:
                params["value"] = value

            label = f"our {var_name}"
            if value:
                label += f" = {value}"

            b = Block(
                type=BLOCK_OUR_VAR,
                label=label,
                params=params,
                children=[]
            )
            add_block(b)
            continue

        m = re_bucket_set.match(stripped)
        if m:
            key, value = m.groups()
            b = Block(
                type=BLOCK_SET_BUCKET,
                label=f'Set bucket "{key}"',
                params={"key": key, "value": value},
                children=[]
            )
            add_block(b)
            continue

        m = re_bucket_get.match(stripped)
        if m:
            var_name, key = m.groups()
            b = Block(
                type=BLOCK_GET_BUCKET,
                label=f'Get bucket "{key}" into {var_name}',
                params={"key": key, "var_name": var_name},
                children=[]
            )
            add_block(b)
            continue

        m = re_bucket_delete.match(stripped)
        if m:
            key = m.group(1)
            b = Block(
                type=BLOCK_DELETE_BUCKET,
                label=f'Delete bucket "{key}"',
                params={"key": key},
                children=[]
            )
            add_block(b)
            continue

        m = re_array_assign.match(stripped)
        if m:
            lhs, value = m.groups()
            lhs = lhs.strip()
            value = value.strip()
            b = Block(
                type=BLOCK_ARRAY_ASSIGN,
                label=f"{lhs} = {value}",
                params={"lhs": lhs, "value": value},
                children=[]
            )
            add_block(b)
            continue

        # plain var assignment (fallback after my/our/buckets)
        m = re_setvar.match(stripped)
        if m:
            var_name, value = m.groups()
            value = value.strip()
            b = Block(
                type=BLOCK_SET_VAR,
                label=f"Set {var_name} = {value}",
                params={"var_name": var_name, "value": value},
                children=[]
            )
            add_block(b)
            continue

        # --- $target->Method(args); ---
        m = re_method.match(stripped)
        if m:
            target, method, args = m.groups()
            args = args.strip()
            label = f"{target}->{method}({args})" if args else f"{target}->{method}()"
            b = Block(
                type=BLOCK_METHOD,
                label=label,
                params={"target": target, "method": method, "args": args},
                children=[]
            )
            add_block(b)
            continue

        # --- return; / return value; ---
        m = re_return.match(stripped)
        if m:
            val = m.group(1).strip()
            label = f"return {val}" if val else "return"
            b = Block(
                type=BLOCK_RETURN,
                label=label,
                params={"value": val},
                children=[]
            )
            add_block(b)
            continue

        # --- Try plugin templates if registry is available ---
        if plugin_patterns:
            matched_plugin = False
            for pdef, pregex in plugin_patterns:
                m = pregex.match(stripped)
                if not m:
                    continue

                plugin_params = m.groupdict()
                label = f"Plugin: {pdef.name} ({pdef.plugin_id})"
                b = Block(
                    type=BLOCK_PLUGIN,
                    label=label,
                    params={
                        "plugin_id": pdef.plugin_id,
                        "plugin_params": plugin_params,
                    },
                    children=[]
                )
                add_block(b)
                matched_plugin = True
                break

            if matched_plugin:
                continue

        # Fallback: any other line becomes a RAW_PERL block
        b = Block(
            type=BLOCK_RAW_PERL,
            label="Raw Perl",
            params={"code": line},
            children=[]
        )
        add_block(b)

    # flush any trailing comments
    flush_comment()
    return blocks


# -------------------------
# Perl code generation
# -------------------------
def generate_perl(blocks: List[Block], plugins: PluginRegistry, npc_id: Optional[int] = None) -> str:
    lines: List[str] = []
    lines.append("# Generated by EQEmu Script Builder v1.2")
    if npc_id is not None:
        lines.append(f"# NPCID: {npc_id}")
    lines.append("")

    def emit(line: str, indent: int = 0):
        lines.append(" " * (4 * indent) + line)

    def render_block(block: Block, indent: int):
        t = block.type

        if t == BLOCK_EVENT:
            event_name = block.params.get("event_name", "EVENT_SAY")
            emit(f"sub {event_name} {{", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)
            emit("", indent)

        elif t == BLOCK_NEXT:
            expr = str(block.params.get("expr", "")).strip()
            if expr:
                emit(f"next {expr};", indent)
            else:
                emit("next;", indent)

        elif t == BLOCK_IF:
            expr = block.params.get("expr", "1")
            emit(f"if ({expr}) {{", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_WHILE:
            expr = block.params.get("expr", "1")
            emit(f"while ({expr}) {{", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_FOREACH:
            var = block.params.get("var_name", "$x")
            lst = block.params.get("list_expr", "@list")
            emit(f"foreach my {var} ({lst}) {{", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_QUEST_CALL:
            fn = block.params.get("quest_fn", "say")
            args = block.params.get("args", '"Hello, world!"')
            emit(f"quest::{fn}({args});", indent)
        
        elif t == BLOCK_ELSIF:
            expr = block.params.get("expr", "1")
            emit(f"elsif ({expr}) {{", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_ELSE:
            emit("else {", indent)
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_SET_VAR:
            var_name = block.params.get("var_name", "$myvar")
            value = block.params.get("value", "0")
            emit(f"{var_name} = {value};", indent)

        elif t == BLOCK_ARRAY_ASSIGN:
            lhs = block.params.get("lhs", "$hash{$key}")
            value = block.params.get("value", "0")
            emit(f"{lhs} = {value};", indent)

        elif t == BLOCK_MY_VAR:
            var_name = block.params.get("var_name", "$myvar")
            value = block.params.get("value", "")

            if value is None:
                value = ""
            value = str(value)

            # Strip exactly ONE trailing semicolon if present
            if value.rstrip().endswith(";"):
                value = value.rstrip()
                value = value[:-1].rstrip()

            if value.strip():
                emit(f"my {var_name} = {value};", indent)
            else:
                emit(f"my {var_name};", indent)

        elif t == BLOCK_OUR_VAR:
            var_name = block.params.get("var_name", "$OurVar")
            value = block.params.get("value", "")

            if value is None:
                value = ""
            value = str(value)

            if value.rstrip().endswith(";"):
                value = value.rstrip()
                value = value[:-1].rstrip()

            if value.strip():
                emit(f"our {var_name} = {value};", indent)
            else:
                emit(f"our {var_name};", indent)

        elif t == BLOCK_SET_BUCKET:
            key = (block.params.get("key") or '"my_bucket"').strip()
            value = (block.params.get("value") or '""').strip()
            emit(f"quest::set_data({key}, {value});", indent)

        elif t == BLOCK_GET_BUCKET:
            key = (block.params.get("key") or '"my_bucket"').strip()
            var_name = (block.params.get("var_name") or "$value").strip()
            emit(f"{var_name} = quest::get_data({key});", indent)

        elif t == BLOCK_DELETE_BUCKET:
            key = (block.params.get("key") or '"my_bucket"').strip()
            emit(f"quest::delete_data({key});", indent)

        elif t == BLOCK_TIMER:
            name = block.params.get("name", "my_timer")
            seconds = int(block.params.get("seconds", 10))
            emit(f'quest::settimer("{name}", {seconds});', indent)

        elif t == BLOCK_FOR:
            var_name = block.params.get("var_name", "$i")

            start   = block.params.get("start", 0)
            end     = block.params.get("end", 10)
            cmp_op  = block.params.get("cmp_op", "<=")
            inc_expr = block.params.get("inc_expr")

            start_s = str(start)
            end_s   = str(end)

            if not inc_expr:
                inc_expr = "++"

            emit(
                f'for (my {var_name} = {start_s}; '
                f'{var_name} {cmp_op} {end_s}; '
                f'{var_name}{inc_expr}) {{',
                indent
            )
            for child in block.children:
                render_block(child, indent + 1)
            emit("}", indent)

        elif t == BLOCK_RETURN:
            val = str(block.params.get("value", "")).strip()
            if val:
                emit(f"return {val};", indent)
            else:
                emit("return;", indent)

        elif t == BLOCK_COMMENT:
            text = block.params.get("text", "")
            if not text.strip():
                emit("#", indent)
            else:
                for line in text.splitlines():
                    emit(f"# {line}", indent)

        elif t == BLOCK_PLUGIN:
            # Render from JSON plugin definition using our safe template renderer
            pid = block.params.get("plugin_id")
            if not pid:
                return

            pdef = plugins.get(pid)
            if not pdef:
                emit(f"# [Unknown plugin: {pid}]", indent)
                return

            plugin_params = block.params.get("plugin_params", {})

            code = render_plugin_template(pdef.perl_template, plugin_params)

            # Emit each line with correct indentation relative to the block
            for ln in code.splitlines():
                # preserve blank lines
                if ln.strip():
                    emit(ln, indent)
                else:
                    emit("", indent)

        elif t == BLOCK_RAW_PERL:
            code = block.params.get("code", "").splitlines()
            for c in code:
                emit(c, indent)

        elif t == BLOCK_METHOD:
            target = block.params.get("target", "$client")
            method = block.params.get("method", "Message")
            args = block.params.get("args", "").strip()
            if args:
                emit(f"{target}->{method}({args});", indent)
            else:
                emit(f"{target}->{method}();", indent)

        else:
            emit(f"# [Unknown block type: {t}]", indent)

    for b in blocks:
        render_block(b, indent=0)

    return "\n".join(lines)

# -------------------------
# Main Application Window
# -------------------------
class EventsConfigDialog(QtWidgets.QDialog):
    """
    Simple dialog: list of all EVENT_* names with checkboxes.
    Lets the user choose which ones appear in the Events menu.
    """
    def __init__(self, all_events, selected_events, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Events")
        self.resize(420, 600)

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "Check the events you want to show in the Events menu "
            "as quick-add options."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )

        selected_set = set(selected_events)

        for ev in sorted(all_events):
            item = QtWidgets.QListWidgetItem(ev)
            item.setFlags(
                item.flags()
                | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                QtCore.Qt.CheckState.Checked
                if ev in selected_set else
                QtCore.Qt.CheckState.Unchecked
            )
            self.list_widget.addItem(item)

        layout.addWidget(self.list_widget)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_events(self) -> List[str]:
        result = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                result.append(item.text())
        return result

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EQEmu Script Builder v1.2")
        self.resize(1400, 750)

        self.templates = BlockTemplateRegistry()

        # Track theme for persistence
        self.current_theme = "dark"

        # Event lists (all vs “common” favorites for the menu)
        self.all_events = list(NPC_EVENTS)
        self.common_events: List[str] = []
        self._load_event_prefs()

        self.registry = PluginRegistry()
        self.methods_by_cat = load_api_methods()

        # Undo / Redo stacks (block-level changes)
        self.undo_stack: List[str] = []
        self.redo_stack: List[str] = []

        # Guard so restoring an undo state doesn't create new undo entries
        self._restoring_undo = False

        # Debounce timer: typing/editing creates ONE undo step, not per-keystroke
        self._undo_debounce_timer = QtCore.QTimer(self)
        self._undo_debounce_timer.setSingleShot(True)
        self._undo_debounce_timer.timeout.connect(self._snapshot_state)


        # Central layout: palettes (tabs) | script+props+diagnostics
        central = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # ------------------------------
        # Left: tabbed palette
        # ------------------------------
        self.palette_tabs = QtWidgets.QTabWidget()
        self.palette_tabs.setTabPosition(QtWidgets.QTabWidget.TabPosition.North)
        self.palette_tabs.setTabShape(QtWidgets.QTabWidget.TabShape.Rounded)
        self.palette_tabs.tabBar().setStyleSheet(
            "QTabBar::tab { padding: 6px 12px; }"
            "QTabBar::tab:selected { font-weight: bold; }"
        )

        # Flow tab (no search)
        self.flow_palette = BlockPalette()
        self.palette_tabs.addTab(self.flow_palette, "Flow")

        # Store method list widgets and their search boxes by category
        self.method_lists: Dict[str, QtWidgets.QListWidget] = {}
        self.method_search_boxes: Dict[str, QtWidgets.QLineEdit] = {}

        desired_cats = [
            "BOT", "BUFF", "CLIENT", "CORPSE", "DOORS",
            "ENTITYLIST", "EXPEDITION", "GROUP", "HATEENTRY",
            "INVENTORY", "MERC", "MOB", "NPC", "OBJECT",
            "RAID", "SPELL", "ZONE"
        ]

        for cat in desired_cats:
            methods = self.methods_by_cat.get(cat, [])
            if not methods:
                continue

            container = QtWidgets.QWidget()
            vbox = QtWidgets.QVBoxLayout(container)
            vbox.setContentsMargins(4, 4, 4, 4)
            vbox.setSpacing(4)

            search = QtWidgets.QLineEdit()
            search.setPlaceholderText(f"Search {cat.title()} methods...")
            vbox.addWidget(search)

            lw = QtWidgets.QListWidget()
            lw.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
            for m in methods:
                sig = m.args
                if len(sig) > 70:
                    sig = sig[:67] + "..."
                text = f"{m.var}->{m.name}({sig})"
                item = QtWidgets.QListWidgetItem(text)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, (m.var, m.name, m.args))
                lw.addItem(item)

            lw.itemDoubleClicked.connect(self.on_method_double_clicked)
            vbox.addWidget(lw)

            def make_filter(list_widget=lw):
                def _filter(text: str):
                    t = text.lower()
                    for i in range(list_widget.count()):
                        item = list_widget.item(i)
                        item.setHidden(t not in item.text().lower())
                return _filter

            search.textChanged.connect(make_filter())

            self.method_lists[cat] = lw
            self.method_search_boxes[cat] = search
            self.palette_tabs.addTab(container, cat.title())

        # ------------------------------
        # Center/right: script tree + props + diagnostics
        # ------------------------------
        self.script_tree = ScriptTree()
        self.props = BlockPropertyEditor(self.registry)

        # Diagnostics (Validation + Live Perl)
        self.diagnostics = DiagnosticsPanel()
        self.diagnostics.go_to_issue_requested.connect(self.on_go_to_issue)
        self.diagnostics.run_perl_c_requested.connect(self.on_run_perl_c_panel)

        # Templates registry
        self.templates = BlockTemplateRegistry()

        # --- Script search UI (center pane) ---
        self.script_search_box = QtWidgets.QLineEdit()
        self.script_search_box.setPlaceholderText("Search blocks by label...")
        self.script_search_next = QtWidgets.QPushButton("Find next")

        script_panel = QtWidgets.QWidget()
        sp_layout = QtWidgets.QVBoxLayout(script_panel)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        sp_layout.setSpacing(4)

        search_bar = QtWidgets.QHBoxLayout()
        search_bar.addWidget(self.script_search_box)
        search_bar.addWidget(self.script_search_next)
        sp_layout.addLayout(search_bar)
        sp_layout.addWidget(self.script_tree)

        # Splitter so user can resize script vs properties
        self.right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.right_splitter.addWidget(script_panel)
        self.right_splitter.addWidget(self.props)
        self.right_splitter.setStretchFactor(0, 4)
        self.right_splitter.setStretchFactor(1, 1)
        self.right_splitter.setSizes([900, 300])

        # OUTER splitter: palette (left) | work area (center) | diagnostics (right)
        outer = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        outer.addWidget(self.palette_tabs)        # left
        outer.addWidget(self.right_splitter)      # center (tree + props)
        outer.addWidget(self.diagnostics)         # right (validation + live perl)

        # --- allow "minimize" of diagnostics via splitter collapse ---
        self.outer_splitter = outer
        self._diag_restore_sizes = None
        self._diag_min_width = 24  # visible "tab" width when minimized (adjust if you want)

        # Let the user fully collapse the diagnostics pane
        outer.setCollapsible(0, False)  # palette_tabs
        outer.setCollapsible(1, False)  # center+props splitter
        outer.setCollapsible(2, True)   # diagnostics

        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 4)
        outer.setStretchFactor(2, 2)
        outer.setSizes([260, 950, 520])

        self.setCentralWidget(outer)

        # ------------------------------
        # Wire up signals
        # ------------------------------
        self.script_tree.structure_changed.connect(self.refresh_diagnostics)
        self.script_tree.structure_changed.connect(self.on_structure_changed)
        self.props.block_changed.connect(lambda _b: self.refresh_diagnostics())

        self.flow_palette.block_selected.connect(self.on_palette_add_block)
        self.script_tree.selection_changed.connect(self.props.set_block)
        self.props.block_changed.connect(self.on_block_changed)

        # Search state for script tree
        self.script_search_matches: List[QtWidgets.QTreeWidgetItem] = []
        self.script_search_index: int = -1
        self.script_search_box.textChanged.connect(self.on_script_search_changed)
        self.script_search_next.clicked.connect(self.on_script_find_next)

        # Menus
        self._create_menu()

        # initial state: either restore last session or start empty
        self._load_script_state()

        # initial diagnostics refresh (after everything is loaded)
        self.refresh_diagnostics()

    def toggle_diagnostics(self):
        """
        Collapse / restore diagnostics pane reliably.
        """
        if not hasattr(self, "outer_splitter"):
            return

        splitter = self.outer_splitter
        sizes = splitter.sizes()

        # Expecting: [palette, center+props, diagnostics]
        if len(sizes) != 3:
            return

        diag_size = sizes[2]

        # ---------- MINIMIZE ----------
        if diag_size > 0:
            # Save exact restore sizes only once
            if not getattr(self, "_diag_restore_sizes", None):
                self._diag_restore_sizes = sizes[:]

            # Collapse diagnostics fully
            new_sizes = sizes[:]
            freed = new_sizes[2]
            new_sizes[2] = 0
            new_sizes[1] += freed  # give space to center pane

            splitter.setSizes(new_sizes)
            return

        # ---------- RESTORE ----------
        if getattr(self, "_diag_restore_sizes", None):
            splitter.setSizes(self._diag_restore_sizes)
            self._diag_restore_sizes = None
        else:
            # Fallback if restore sizes somehow missing
            splitter.setSizes([260, 900, 340])

    def on_delete_template(self):
        templates = self.templates.list_templates()
        if not templates:
            QtWidgets.QMessageBox.information(self, "Templates", "No templates saved yet.")
            return

        names = [f"{t.name}  [{t.template_id}]" for t in templates]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Delete Template", "Template:", names, 0, False
        )
        if not ok:
            return

        tid = choice.rsplit("[", 1)[-1].rstrip("]")
        t = self.templates.templates[tid]

        confirm = QtWidgets.QMessageBox.question(
            self,
            "Confirm Delete",
            f"Delete template:\n\n{t.name}\n\nThis cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.templates.delete(t.template_id)
        QtWidgets.QMessageBox.information(self, "Templates", f"Deleted: {t.name}")

    def on_save_template(self):
        item = self.script_tree.currentItem()
        if not item:
            return
        block: Block = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        name, ok = QtWidgets.QInputDialog.getText(self, "Template Name", "Name:")
        if not ok or not name.strip():
            return

        tid = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip()).lower()
        self.templates.templates[tid] = BlockTemplateDef(
            template_id=tid,
            name=name.strip(),
            root_block=block.to_dict(),
        )
        self.templates.save()
        self.refresh_diagnostics()
        
    def ensure_event_timer_handler(self, timer_name: str, old_name: str | None = None, prefer_after_event: str = ""):
        timer_name = (timer_name or "").strip()
        if not timer_name:
            return

        old_name = (old_name or "").strip() or None

        blocks = self.script_tree.rebuild_block_tree()

        # ----------------------------
        # Find EVENT_TIMER top-level
        # ----------------------------
        event_timer_idx = None
        event_timer = None
        for i, b in enumerate(blocks):
            if b.type == BLOCK_EVENT and (b.params.get("event_name") or "").strip() == "EVENT_TIMER":
                event_timer_idx = i
                event_timer = b
                break

        # Create if missing
        if event_timer is None:
            event_timer = Block(
                type=BLOCK_EVENT,
                label="EVENT_TIMER",
                params={"event_name": "EVENT_TIMER"},
                children=[]
            )
            blocks.append(event_timer)
        else:
            # Force EVENT_TIMER to be last top-level block
            if event_timer_idx is not None and event_timer_idx != len(blocks) - 1:
                blocks.pop(event_timer_idx)
                blocks.append(event_timer)

        def _expr_for(name: str) -> str:
            return f'$timer eq "{name}"'

        def _ensure_stoptimer_quest_call(if_block: Block, tname: str):
            want_fn = "stoptimer"
            want_args = f'"{tname}"'

            # Update existing quest::stoptimer if present
            for ch in (if_block.children or []):
                if ch.type == BLOCK_QUEST_CALL and (ch.params.get("quest_fn") or "") == want_fn:
                    ch.params["args"] = want_args
                    ch.label = f'quest::{want_fn}({want_args})'
                    return


            # Insert if missing
            q = Block(
                type=BLOCK_QUEST_CALL,
                label=f'quest::{want_fn}({want_args})',
                params={"quest_fn": want_fn, "args": want_args},
                children=[]
            )
            if_block.children = (if_block.children or [])
            if_block.children.insert(0, q)

        # ----------------------------
        # 1) If exact branch exists, keep it (but ensure quest::stoptimer matches)
        # ----------------------------
        want_expr = _expr_for(timer_name)
        for c in event_timer.children:
            if c.type == BLOCK_IF and (c.params.get("expr") or "").strip() == want_expr:
                _ensure_stoptimer_quest_call(c, timer_name)
                self.script_tree.load_from_blocks(blocks)
                self.script_tree._emit_structure_changed()
                return

        # ----------------------------
        # 2) If we're renaming, rewrite the old branch instead of adding a new one
        # ----------------------------
        if old_name:
            old_expr = _expr_for(old_name)
            for c in event_timer.children:
                if c.type == BLOCK_IF and (c.params.get("expr") or "").strip() == old_expr:
                    c.params["expr"] = want_expr
                    c.label = f'if ({want_expr})'
                    _ensure_stoptimer_quest_call(c, timer_name)

                    self.script_tree.load_from_blocks(blocks)
                    self.script_tree._emit_structure_changed()
                    return

        # ----------------------------
        # 3) Otherwise create a new branch
        # ----------------------------
        if_block = Block(
            type=BLOCK_IF,
            label=f'if ({want_expr})',
            params={"expr": want_expr},
            children=[
                Block(
                    type=BLOCK_QUEST_CALL,
                    label=f'quest::stoptimer("{timer_name}")',
                    params={"quest_fn": "stoptimer", "args": f'"{timer_name}"'},
                    children=[]
                ),
                Block(
                    type=BLOCK_RAW_PERL,
                    label="Raw Perl",
                    params={"code": "# TODO: your timer logic here\n"},
                    children=[]
                )
            ]
        )
        event_timer.children.append(if_block)

        self.script_tree.load_from_blocks(blocks)
        self.script_tree._emit_structure_changed()




    def on_insert_template(self):
        templates = self.templates.list_templates()
        if not templates:
            QtWidgets.QMessageBox.information(self, "Templates", "No templates saved yet.")
            return

        names = [t.name for t in templates]
        choice, ok = QtWidgets.QInputDialog.getItem(self, "Insert Template", "Template:", names, 0, False)
        if not ok:
            return

        t = next(x for x in templates if x.name == choice)
        block = Block.from_dict(t.root_block)

        parent = self.script_tree.currentItem()
        self.script_tree.insert_block_object(block, parent_item=parent)
        self.script_tree._emit_structure_changed()

    def refresh_diagnostics(self):
        blocks = self.script_tree.rebuild_block_tree()

        issues = validate_blocks(blocks, self.registry)
        self.diagnostics.set_validation(issues)
        
        self.script_tree.clear_issue_markers()
        self.script_tree.apply_issue_markers(issues)


        try:
            perl = generate_perl(blocks, self.registry)
        except Exception as e:
            perl = f"# Live preview error:\n# {e}\n"
        self.diagnostics.set_perl(perl)

    def run_perl_check_into_panel(self):
        """
        On-demand perl -c, writes results into the DiagnosticsPanel tab (no modal).
        """
        blocks = self.script_tree.rebuild_block_tree()
        perl = generate_perl(blocks, self.registry)

        import subprocess
        try:
            proc = subprocess.run(
                ["perl", "-c", "-"],
                input=perl.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except FileNotFoundError:
            self.diagnostics.set_perl_check(
                "Perl not found.\n\nInstall Perl and ensure `perl` is in PATH.",
                ok=False
            )
            return

        output = proc.stdout.decode("utf-8", errors="ignore")
        self.diagnostics.set_perl_check(output, ok=(proc.returncode == 0))

    # --- Script tree search / highlight ---

    def _clear_tree_highlights(self):
        def visit(item: QtWidgets.QTreeWidgetItem):
            item.setBackground(0, QtGui.QBrush())
            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.script_tree.topLevelItemCount()):
            visit(self.script_tree.topLevelItem(i))

    def _collect_matching_items(self, text: str) -> List[QtWidgets.QTreeWidgetItem]:
        matches: List[QtWidgets.QTreeWidgetItem] = []
        if not text:
            return matches
        t = text.lower()

        def visit(item: QtWidgets.QTreeWidgetItem):
            if t in item.text(0).lower():
                matches.append(item)
            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.script_tree.topLevelItemCount()):
            visit(self.script_tree.topLevelItem(i))
        return matches

    def on_script_search_changed(self, text: str):
        self._clear_tree_highlights()
        self.script_search_matches = self._collect_matching_items(text)
        self.script_search_index = 0 if self.script_search_matches else -1

        if not self.script_search_matches:
            return

        highlight_brush = QtGui.QBrush(QtGui.QColor(138, 180, 248, 60))
        for item in self.script_search_matches:
            item.setBackground(0, highlight_brush)

        first = self.script_search_matches[0]
        self.script_tree.setCurrentItem(first)
        self.script_tree.scrollToItem(first)

    def on_script_find_next(self):
        if not self.script_search_matches:
            return
        self.script_search_index = (self.script_search_index + 1) % len(self.script_search_matches)
        item = self.script_search_matches[self.script_search_index]
        self.script_tree.setCurrentItem(item)
        self.script_tree.scrollToItem(item)

    
    # --- Undo/Redo helpers ---

    def _serialize_state(self) -> str:
        blocks = self.script_tree.rebuild_block_tree()
        return json.dumps([b.to_dict() for b in blocks])

    def _restore_state(self, state: str):
        self._restoring_undo = True
        try:
            data = json.loads(state)
            blocks = [Block.from_dict(d) for d in data]
            self.script_tree.load_from_blocks(blocks)
            self.props.clear_form()
            self.refresh_diagnostics()
        finally:
            self._restoring_undo = False

    def _snapshot_state(self):
        """
        Capture current script structure into the undo stack.
        Avoid pushing duplicate consecutive states.
        """
        state = self._serialize_state()

        # If nothing has changed structurally, don't add a new undo step
        if self.undo_stack and self.undo_stack[-1] == state:
            return

        self.undo_stack.append(state)
        self.redo_stack.clear()


    def _save_script_state(self):
        """
        Persist script blocks + window layout to SCRIPT_STATE_FILE.
        """
        try:
            # Blocks (as dicts)
            blocks_json = json.loads(self._serialize_state())
        except Exception:
            blocks_json = []

        # Window geometry/state
        geom_ba = self.saveGeometry()
        state_ba = self.saveState()

        state = {
            "blocks": blocks_json,
            "geometry": bytes(geom_ba.toHex()).decode("ascii"),
            "window_state": bytes(state_ba.toHex()).decode("ascii"),
            "palette_tab": self.palette_tabs.currentIndex(),
            "splitter_sizes": self.right_splitter.sizes(),
            "theme": self.current_theme,
        }

        try:
            with open(SCRIPT_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            # Silent fail is fine here
            pass


    def _load_script_state(self):
        """
        Restore script blocks + window layout from SCRIPT_STATE_FILE if present.
        """
        if not os.path.exists(SCRIPT_STATE_FILE):
            # Nothing saved, keep the empty initial state
            self._snapshot_state()
            return

        try:
            with open(SCRIPT_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Corrupt or unreadable state; start fresh
            self._snapshot_state()
            return

        # Restore blocks
        blocks_data = data.get("blocks")
        if isinstance(blocks_data, list):
            try:
                blocks = [Block.from_dict(d) for d in blocks_data]
                self.script_tree.load_from_blocks(blocks)
                self.props.clear_form()
            except Exception:
                # If something blows up, just ignore and keep empty
                self.script_tree.clear_script()
                self.props.clear_form()

        # Restore geometry
        geom_hex = data.get("geometry")
        if isinstance(geom_hex, str):
            try:
                ba = QtCore.QByteArray.fromHex(geom_hex.encode("ascii"))
                self.restoreGeometry(ba)
            except Exception:
                pass

        # Restore window state (menus/toolbars, if you add any later)
        state_hex = data.get("window_state")
        if isinstance(state_hex, str):
            try:
                ba = QtCore.QByteArray.fromHex(state_hex.encode("ascii"))
                self.restoreState(ba)
            except Exception:
                pass

        # Restore palette tab
        idx = data.get("palette_tab")
        if isinstance(idx, int) and 0 <= idx < self.palette_tabs.count():
            self.palette_tabs.setCurrentIndex(idx)

        # Restore splitter sizes
        sizes = data.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self.right_splitter.setSizes([int(sizes[0]), int(sizes[1])])
            except Exception:
                pass

        # Restore theme
        theme = data.get("theme")
        app = QtWidgets.QApplication.instance()
        if app is not None and theme in ("dark", "light"):
            if theme == "dark":
                apply_dark_theme(app)
                self.current_theme = "dark"
            else:
                apply_light_theme(app)
                self.current_theme = "light"

        # Reset undo/redo stacks to this restored state
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._snapshot_state()

        # --- Event menu prefs ---

    def _load_event_prefs(self):
        self.common_events = sorted(set(DEFAULT_COMMON_EVENTS) & set(self.all_events))
        try:
            if os.path.exists(EVENT_CONFIG_FILE):
                with open(EVENT_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                stored = data.get("common_events")
                if isinstance(stored, list):
                    # only keep valid events
                    self.common_events = sorted(
                        ev for ev in stored if ev in self.all_events
                    )
        except Exception:
            # ignore any parse errors and fall back to defaults
            pass

        if not self.common_events:
            # make sure it's never empty
            self.common_events = sorted(set(DEFAULT_COMMON_EVENTS) & set(self.all_events))

    def _save_event_prefs(self):
        try:
            with open(EVENT_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"common_events": self.common_events},
                    f,
                    indent=2
                )
        except Exception:
            # silent fail is fine for a helper
            pass

    def _rebuild_events_menu(self):
        self.events_menu.clear()

        if self.common_events:
            for ev in self.common_events:
                act_ev = self.events_menu.addAction(ev)
                act_ev.triggered.connect(partial(self.on_add_event, ev))
        else:
            placeholder = self.events_menu.addAction("(No events selected)")
            placeholder.setEnabled(False)

        self.events_menu.addSeparator()
        act_cfg = self.events_menu.addAction("Configure Events…")
        act_cfg.triggered.connect(self.on_configure_events)

    def on_configure_events(self):
        dlg = EventsConfigDialog(self.all_events, self.common_events, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            new_sel = dlg.selected_events()
            # Alphabetize and dedupe
            self.common_events = sorted(set(new_sel) & set(self.all_events))
            self._save_event_prefs()
            self._rebuild_events_menu()

    # --- Menu setup ---

    def _create_menu(self):
        bar = self.menuBar()
        tmpl_menu = bar.addMenu("Templates")
        
        file_menu = bar.addMenu("&File")
        act_new = file_menu.addAction("New Script")
        act_open = file_menu.addAction("Open Perl...")
        act_export = file_menu.addAction("Export Perl...")
        file_menu.addSeparator()
        act_quit = file_menu.addAction("Quit")
        act_save_tmpl = tmpl_menu.addAction("Save Selected Block As Template")
        act_insert_tmpl = tmpl_menu.addAction("Insert Template…")
        tmpl_menu.addSeparator()
        act_delete_tmpl = tmpl_menu.addAction("Delete Template…")
        
        act_new.triggered.connect(self.on_new_script)
        act_open.triggered.connect(self.on_open_perl)
        act_export.triggered.connect(self.on_export_perl)
        act_quit.triggered.connect(QtWidgets.QApplication.quit)
        act_save_tmpl.triggered.connect(self.on_save_template)
        act_insert_tmpl.triggered.connect(self.on_insert_template)
        act_delete_tmpl.triggered.connect(self.on_delete_template)

        # Edit menu: Undo / Redo / Delete
        edit_menu = bar.addMenu("&Edit")
        act_undo = edit_menu.addAction("Undo")
        act_redo = edit_menu.addAction("Redo")
        edit_menu.addSeparator()
        act_delete = edit_menu.addAction("Delete Block")

        act_undo.setShortcut(QtGui.QKeySequence.StandardKey.Undo)
        act_redo.setShortcut(QtGui.QKeySequence.StandardKey.Redo)
        act_delete.setShortcut(QtGui.QKeySequence.StandardKey.Delete)

        act_undo.triggered.connect(self.on_undo)
        act_redo.triggered.connect(self.on_redo)
        act_delete.triggered.connect(self.on_delete_block)

        # Events menu
        self.events_menu = bar.addMenu("&Events")
        self._rebuild_events_menu()


        # Plugins menu
        plugin_menu = bar.addMenu("&Plugins")
        act_manage = plugin_menu.addAction("Manage Plugins...")
        act_manage.setShortcut("Alt+P")
        act_manage.triggered.connect(self.on_manage_plugins)

        # View menu: theme switching
        view_menu = bar.addMenu("&View")
        act_toggle_diag = view_menu.addAction("Toggle Diagnostics")
        act_toggle_diag.setShortcut("F8")
        act_toggle_diag.triggered.connect(self.toggle_diagnostics)
        view_menu.addSeparator()
        act_dark = view_menu.addAction("Dark Theme")
        act_light = view_menu.addAction("Light Theme")

        act_dark.triggered.connect(self.on_set_dark_theme)
        act_light.triggered.connect(self.on_set_light_theme)
        
        # Tools menu: Perl syntax check
        tools_menu = bar.addMenu("&Tools")
        act_check_perl = tools_menu.addAction("Check Perl Syntax")
        act_check_perl.setStatusTip("Run 'perl -c' on the generated script")
        act_check_perl.triggered.connect(self.on_check_perl)
   
    # --- Undo / Redo actions ---

    def _queue_snapshot_state(self, delay_ms: int = 350):
        """
        Schedule an undo snapshot.
        - For edits: debounce (delay_ms > 0) so typing becomes ONE undo step.
        - For structural changes: call with 0 to snapshot immediately.
        """
        if getattr(self, "_restoring_undo", False):
            return

        if delay_ms <= 0:
            self._snapshot_state()
            return

        # Restart debounce timer
        if hasattr(self, "_undo_debounce_timer") and self._undo_debounce_timer:
            self._undo_debounce_timer.start(delay_ms)
        else:
            # Fallback (shouldn't happen)
            self._snapshot_state()

    def on_structure_changed(self):
        """
        Called when ScriptTree structure changes (insert/move/remove/drag-drop).
        These should become a single undo step.
        """
        self._queue_snapshot_state(0)

    def on_undo(self):
        if len(self.undo_stack) <= 1:
            return
        current = self.undo_stack.pop()
        self.redo_stack.append(current)
        prev = self.undo_stack[-1]
        self._restore_state(prev)

    def on_redo(self):
        if not self.redo_stack:
            return
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        self._restore_state(state)

    # --- Slots ---

    def on_set_dark_theme(self):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            apply_dark_theme(app)
        self.current_theme = "dark"

    def on_set_light_theme(self):
        app = QtWidgets.QApplication.instance()
        if app is not None:
            apply_light_theme(app)
        self.current_theme = "light"

    def on_add_event(self, event_name: str):
        # Always top-level
        block, item = self.script_tree.add_block(BLOCK_EVENT, parent_item=None)
        block.params["event_name"] = event_name
        block.label = event_name
        self.script_tree.update_item_label(block)
        self.script_tree.setCurrentItem(item)
        self.props.set_block(block)
        self._snapshot_state()

    def on_palette_add_block(self, block_type: str):
        # Add as child of selected block if possible, else as top-level
        item = self.script_tree.currentItem()
        parent_item = item

        # Some blocks should be top-level only (events)
        if block_type == BLOCK_EVENT:
            parent_item = None

        block, tree_item = self.script_tree.add_block(block_type, parent_item)
        self.props.set_block(block)
        self._snapshot_state()

    def on_method_double_clicked(self, item: QtWidgets.QListWidgetItem):
        data = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not data:
            return
        var, name, args = data

        parent_item = self.script_tree.currentItem()
        block, _tree_item = self.script_tree.add_block(BLOCK_METHOD, parent_item=parent_item)
        block.params["target"] = var
        block.params["method"] = name
        block.params["args"] = args
        if args.strip():
            block.label = f"{var}->{name}({args})"
        else:
            block.label = f"{var}->{name}()"
        self.script_tree.update_item_label(block)
        self.props.set_block(block)
        self._snapshot_state()
        
    def on_run_perl_c_panel(self):
        # Generate Perl
        try:
            blocks = self.script_tree.rebuild_block_tree()
            perl = generate_perl(blocks, self.registry)
        except Exception as e:
            self.diagnostics.set_perl_c_output_text(f"# Failed to generate Perl:\n# {e}\n")
            self.diagnostics.set_perl_c_status(False)
            return

        import subprocess
        import shutil

        perl_exe = shutil.which("perl")
        if not perl_exe:
            self.diagnostics.set_perl_c_output_text(
                "Perl not found in PATH.\n\n"
                "Install Strawberry Perl and ensure perl.exe is in PATH."
            )
            self.diagnostics.set_perl_c_status(False)
            return

        try:
            proc = subprocess.run(
                [perl_exe, "-c", "-"],
                input=perl.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            output = proc.stdout.decode("utf-8", errors="ignore")
        except Exception as e:
            self.diagnostics.set_perl_c_output_text(f"Failed to run perl -c:\n{e}")
            self.diagnostics.set_perl_c_status(False)
            return

        self.diagnostics.set_perl_c_output_text(output)
        self.diagnostics.set_perl_c_status(proc.returncode == 0)

        # Optional: switch to the Perl -c tab automatically
        self.diagnostics.setCurrentIndex(2)  # 0=Validation, 1=Live Perl, 2=Perl -c

    def on_check_perl(self):
        print("DEBUG: on_check_perl fired")
        """
        Generate Perl and run 'perl -c' to check syntax.
        Shows stdout/stderr in a dialog.
        """
        try:
            blocks = self.script_tree.rebuild_block_tree()
            perl = generate_perl(blocks, self.registry)
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Perl Generation Error",
                f"Failed to generate Perl:\n\n{e}"
            )
            return

        import subprocess
        import shutil

        perl_exe = shutil.which("perl")
        if not perl_exe:
            QtWidgets.QMessageBox.warning(
                self,
                "Perl not found",
                "Perl was not found in PATH.\n\n"
                "Install Strawberry Perl or add perl.exe to PATH."
            )
            return

        try:
            proc = subprocess.run(
                [perl_exe, "-c", "-"],
                input=perl.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Execution Error",
                f"Failed to run perl:\n\n{e}"
            )
            return

        output = proc.stdout.decode("utf-8", errors="ignore")

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Perl Syntax Check")
        dlg.resize(900, 500)

        layout = QtWidgets.QVBoxLayout(dlg)

        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(output)
        layout.addWidget(text)

        status = QtWidgets.QLabel()
        status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        if proc.returncode == 0:
            status.setText("<b style='color:green;'>Perl syntax OK</b>")
        else:
            status.setText("<b style='color:red;'>Perl syntax errors detected</b>")
        layout.addWidget(status)

        btn = QtWidgets.QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        dlg.exec()

    def on_block_changed(self, block: Block):
        self.script_tree.update_item_label(block)
        # Block-level undo: debounce so typing becomes one undo step
        self._queue_snapshot_state(350)

    def on_go_to_issue(self, issue: ValidationIssue):
        b = getattr(issue, "block_ref", None)
        if b is None:
            return
        self.script_tree.select_block(b)
        self.script_tree.setFocus()

    def on_delete_block(self):
        self.script_tree.delete_current()
        self.props.clear_form()
        self._snapshot_state()

    def on_new_script(self):
        if self.script_tree.topLevelItemCount() > 0:
            if QtWidgets.QMessageBox.question(
                self, "Clear script?", "Start a new script?"
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.script_tree.clear_script()
        self.props.clear_form()
        self._snapshot_state()

    def on_open_perl(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open Perl Script",
            "",
            "Perl Scripts (*.pl *.pm);;All Files (*)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Error",
                f"Failed to read file:\n{e}"
            )
            return

        blocks = parse_perl_to_blocks(src, self.registry)
        if not blocks:
            QtWidgets.QMessageBox.warning(
                self,
                "Nothing parsed",
                "No recognizable blocks were found in that file.\n"
                "The parser is intentionally conservative and mostly targets\n"
                "code that was originally generated by this tool."
            )
            return

        # Load into the tree
        self.script_tree.load_from_blocks(blocks)
        self.props.clear_form()

        # Reset undo/redo stacks to this imported state
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._snapshot_state()

    def on_export_perl(self):
        blocks = self.script_tree.rebuild_block_tree()
        perl = generate_perl(blocks, self.registry)

        # Show in dialog and optionally save to file
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Generated Perl Script")
        dlg.resize(800, 600)
        v = QtWidgets.QVBoxLayout(dlg)
        text = QtWidgets.QPlainTextEdit()
        text.setPlainText(perl)
        v.addWidget(text)
        hl = QtWidgets.QHBoxLayout()
        btn_save = QtWidgets.QPushButton("Save to file...")
        btn_close = QtWidgets.QPushButton("Close")
        hl.addWidget(btn_save)
        hl.addWidget(btn_close)
        v.addLayout(hl)

        def do_save():
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                dlg, "Save Perl Script", "npc_quest.pl",
                "Perl Script (*.pl);;All Files (*)"
            )
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.toPlainText())

        btn_save.clicked.connect(do_save)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def on_manage_plugins(self):
        dlg = PluginManagerDialog(self.registry, self)
        dlg.exec()

    def closeEvent(self, event: QtGui.QCloseEvent):
        # Save script + layout on exit
        try:
            self._save_script_state()
        except Exception:
            pass
        super().closeEvent(event)

# -------------------------
# main
# -------------------------

def main():
    import sys
    app = QtWidgets.QApplication(sys.argv)

    # Apply modern dark theme + stylesheet
    apply_modern_theme(app)

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

