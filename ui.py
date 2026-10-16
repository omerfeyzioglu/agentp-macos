#!/usr/bin/env python3
"""
ui.py — AgentP's panel: opens anywhere with ⌥Space, follows the system light/dark appearance.

  ⌥Space     show / hide (the agent starts from the app you are in)
  🌐 🌐       macOS Dictation: say the command, Return to run it
  Esc        hide the panel, or cancel a pending question
  Menu bar   "P": show / mark last request wrong / clear context / quit

The panel does not steal focus (like Spotlight). Confirmations and "which one?"
questions appear inline at the bottom of the panel: Return accepts, Esc cancels.

Usage:
  ./run.sh            (or: python3 ui.py)
"""
import os
import re
import subprocess
import sys
import threading

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBox,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskKeyDown,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSFloatingWindowLevel,
    NSFont,
    NSCursor,
    NSCursorAttributeName,
    NSFontAttributeName,
    NSLinkAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageView,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSMutableParagraphStyle,
    NSPanel,
    NSParagraphStyleAttributeName,
    NSProgressIndicator,
    NSRunningApplication,
    NSScreen,
    NSScrollView,
    NSSecureTextField,
    NSStatusBar,
    NSTextField,
    NSTextView,
    NSVariableStatusItemLength,
    NSView,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject
from PyObjCTools import AppHelper
from typesafe_sdk import TypeSafeClient

import agent
import qa
import config

KEY_SPACE, KEY_ESC = 49, 53
W, H = 640, 440
PAD = 20
INPUT_H = 64
RADIUS = 14


# ---------------------------------------------------------------- text styles
def styled(text: str, size: float = 13, weight: float = 0.0, color=None, mono: bool = False,
           space_before: float = 0, indent: float = 0, hang: bool = True) -> NSAttributedString:
    font = (NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if mono
            else NSFont.systemFontOfSize_weight_(size, weight))
    para = NSMutableParagraphStyle.alloc().init()
    para.setLineSpacing_(2)
    para.setParagraphSpacingBefore_(space_before)
    para.setFirstLineHeadIndent_(indent)
    para.setHeadIndent_(indent + 16 if indent and hang else indent)   # hang: wrapped lines align after "✓  "
    return NSAttributedString.alloc().initWithString_attributes_(text + "\n", {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: color or NSColor.labelColor(),
        NSParagraphStyleAttributeName: para,
    })


def friendly(line: str):
    """Turn the agent's terminal-style output into clean rows. None = hide the line."""
    s = line.strip()
    if not s or "(dry-run)" in s:
        return None
    if re.match(r"^\d+\. \w+ ?(→|←|\[)", s) or re.match(r"^\d+\. \w+\s+\[last step", s):
        return None                          # "1. click → … [last step: 0.12]" — decision detail
    if s.startswith("› "):
        return styled(s[2:], 14, 0.5, space_before=14)
    if s.startswith("✓ "):
        return styled("✓  " + sentence(s[2:]), 13, 0, NSColor.labelColor(), indent=2)
    if s.startswith("✗ "):
        return styled("✕  " + sentence(s[2:]), 13, 0, NSColor.systemRedColor(), indent=2)
    if s.startswith("💬"):
        return styled(s.lstrip("💬 ").strip(), 13.5, 0, NSColor.labelColor(), space_before=4, indent=2)
    if s.startswith("⏱"):
        m = re.match(r"⏱ (\d+) ms", s)
        took = f"{int(m.group(1)) / 1000:.1f} s" if m else s
        return styled(took, 11, 0, NSColor.tertiaryLabelColor(), mono=True, space_before=2, indent=2)
    if s.startswith("plan"):
        return styled("Plan" + (" (revised)" if "revised" in s else ""), 11, 0.5,
                      NSColor.secondaryLabelColor(), space_before=4, indent=2)
    if re.match(r"^\d+\. \w+: ", s):         # plan steps: "1. open_app: Open WhatsApp (WhatsApp)"
        n, rest = s.split(". ", 1)
        rest = re.sub(r"^\w+: ", "", rest)
        m = re.search(r" \(([^()]*)\)", rest)   # "(WhatsApp)" repeats the step; keep URLs only
        if m and "/" not in m.group(1):
            rest = rest.replace(m.group(0), "", 1)
        return styled(f"{n}   {rest}", 12, 0, NSColor.secondaryLabelColor(), mono=True, indent=10)
    if s.startswith("⚠"):
        return styled(s.lstrip("⚠ ").strip(), 13, 0, NSColor.systemOrangeColor(), indent=2)
    if s.startswith("("):
        return styled(s.strip("()"), 11, 0, NSColor.tertiaryLabelColor(), indent=2)
    return styled(s, 12.5, 0, NSColor.secondaryLabelColor(), indent=2)


def sentence(text: str) -> str:
    """'typed 'hi' (Compose message | ChatBar…)' → "Typed 'hi'" """
    text = re.sub(r" \([^()]*\)$", "", text.strip())
    return text[:1].upper() + text[1:]


def plain_option(o: str) -> str:
    """'button: Alex | Members: Sam, You | NavigationBar_HeaderViewButton [top]' → 'Alex — Members: Sam, You'"""
    o = re.sub(r"\s*\[[\w ]+\]$", "", o)                 # position
    o = re.sub(r"^[\w /]+?: ", "", o, count=1)              # role
    parts = [p.strip() for p in o.split("|")]
    parts = [p for p in parts if p and not re.fullmatch(r"[A-Za-z]+(_[A-Za-z]+)+|[a-z]+[A-Z]\w*", p)]
    text = " — ".join(dict.fromkeys(parts)) or o
    return text if len(text) <= 70 else text[:69] + "…"


def plan_step_text(s: str) -> str:
    """'3. type: Type the message ← 'On my way!'' → "Type the message ← 'On my way!'" (no numbering)"""
    rest = re.sub(r"^\d+\.\s+\w+:\s+", "", s.strip())
    m = re.search(r" \(([^()]*)\)", rest)   # "(WhatsApp)" repeats the step; keep URLs only
    if m and "/" not in m.group(1):
        rest = rest.replace(m.group(0), "", 1)
    return rest


def tidy_question(msg: str) -> str:
    """'Send? [TEXT BOX: Compose message | contains: 'hi']' → 'Send “hi”?'"""
    m = re.search(r"contains: '(.*)'\]?$", msg) or re.search(r'contains: "(.*)"\]?$', msg)
    if msg.startswith("Send?"):
        return f"Send “{m.group(1)}”?" if m else "Send?"
    return re.sub(r"\s*\[.*\]\s*$", "", msg)


# ---------------------------------------------------------------- views
class KeyPanel(NSPanel):
    """Borderless panels cannot become key by default; this one can (to type into it). While the
    agent works, it gives the keyboard back so keys reach the app being operated."""

    lets_go_of_keyboard = False

    def canBecomeKeyWindow(self):
        return not self.lets_go_of_keyboard


class Background(NSView):
    """Rounded card in the system text background colour (white / near-black) with a hairline border."""

    def drawRect_(self, rect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, self.bounds().size.width - 1, self.bounds().size.height - 1), RADIUS, RADIUS)
        NSColor.textBackgroundColor().setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1)
        path.stroke()


class Sheet(NSView):
    """A full-width layer below the input (API keys): square top edge, rounded bottom corners, so the
    panel's own border stays the only outline."""

    def drawRect_(self, rect):
        w, h = self.bounds().size.width, self.bounds().size.height
        NSColor.textBackgroundColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0, 0, w, h), RADIUS - 1, RADIUS - 1).fill()
        NSBezierPath.fillRect_(NSMakeRect(0, RADIUS, w, h - RADIUS))


class PanelIO:
    """Same interface as agent.CliIO, through the panel (called from the worker thread)."""

    def __init__(self, ui):
        self.ui = ui

    def say(self, text: str):
        for line in text.split("\n"):
            AppHelper.callAfter(self.ui.append, line)

    def stream(self, delta: str):
        AppHelper.callAfter(self.ui.answer_delta, delta)

    def stream_end(self, sources: list, suggestions: list = ()):
        AppHelper.callAfter(self.ui.answer_done, sources, list(suggestions))

    def _ask(self, question: str, buttons: list[str], default: int, cancel: int) -> int:
        done, box = threading.Event(), {}

        def answer(i):
            box["i"] = i
            done.set()

        AppHelper.callAfter(self.ui.ask, question, buttons, default, cancel, answer)
        done.wait()
        return box["i"]

    def confirm(self, msg: str, default_yes: bool = False) -> bool:
        q = tidy_question(msg)
        yes = "Send" if q.startswith("Send") else "Yes"
        return self._ask(q, ["Cancel", yes], default=1 if default_yes else 0, cancel=0) == 1

    def pick(self, question: str, options: list[str]) -> int | None:
        labels = [plain_option(o) for o in options]
        i = self._ask(question, ["Cancel"] + labels, default=1, cancel=0)
        return i - 1 if i > 0 else None


class UI(NSObject):
    def init(self):
        self = objc.super(UI, self).init()
        self.busy = False
        self.answer_cb = None
        self.entries, self.collecting = [], None   # activity log model (rows + collapsible plans)
        self.client = TypeSafeClient() if not config.missing_required() else None
        self.ctx = agent.make_ctx(io=PanelIO(self))
        self.ctx.own_app = str(NSRunningApplication.currentApplication().localizedName())
        self._build_panel()
        self._build_status_item()
        self._install_hotkeys()
        return self

    # ------------------------------------------------ panel
    @objc.python_method
    def _build_panel(self):
        frame = NSScreen.mainScreen().visibleFrame()
        x = frame.origin.x + (frame.size.width - W) / 2
        y = frame.origin.y + frame.size.height * 0.58
        p = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        p.setLevel_(NSFloatingWindowLevel)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setHidesOnDeactivate_(False)
        p.setMovableByWindowBackground_(True)
        p.setBecomesKeyOnlyIfNeeded_(False)

        root = Background.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        p.setContentView_(root)

        field = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, H - INPUT_H + 14, W - PAD * 2 - 36, 34))
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(1)   # none
        field.setFont_(NSFont.systemFontOfSize_weight_(21, 0.0))
        field.setTextColor_(NSColor.labelColor())
        field.setPlaceholderAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            "What should I do?", {NSFontAttributeName: NSFont.systemFontOfSize_weight_(21, 0.0),
                                  NSForegroundColorAttributeName: NSColor.tertiaryLabelColor()}))
        field.setTarget_(self)
        field.setAction_("submit:")
        root.addSubview_(field)

        mic = NSImageView.alloc().initWithFrame_(NSMakeRect(W - PAD - 22, H - INPUT_H + 21, 20, 20))
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic", "Dictation")
        if img is not None:
            mic.setImage_(img)
            mic.setContentTintColor_(NSColor.tertiaryLabelColor())
        mic.setToolTip_("Press 🌐 twice to dictate")
        root.addSubview_(mic)

        spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(W - PAD - 20, H - INPUT_H + 22, 16, 16))
        spinner.setStyle_(1)            # spinning
        spinner.setControlSize_(1)      # small
        spinner.setDisplayedWhenStopped_(False)
        root.addSubview_(spinner)

        sep = NSBox.alloc().initWithFrame_(NSMakeRect(0, H - INPUT_H, W, 1))
        sep.setBoxType_(2)              # separator
        root.addSubview_(sep)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD - 4, 12, W - PAD * 2 + 8, H - INPUT_H - 18))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        log = NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
        log.setEditable_(False)
        log.setSelectable_(True)
        log.setDrawsBackground_(False)
        log.setDelegate_(self)
        log.setLinkTextAttributes_({NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                                    NSCursorAttributeName: NSCursor.pointingHandCursor()})
        log.setTextContainerInset_((4, 8))
        scroll.setDocumentView_(log)
        root.addSubview_(scroll)

        prompt = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, 0))
        prompt.setHidden_(True)
        root.addSubview_(prompt)

        self.panel, self.field, self.log, self.scroll = p, field, log, scroll
        self.mic, self.spinner, self.prompt, self.root = mic, spinner, prompt, root

    @objc.python_method
    def append(self, line: str):
        """Add one line of agent output. Plans are collected into one collapsible entry."""
        s = line.strip()
        if s.startswith("plan"):
            self.collecting = {"type": "plan", "steps": [], "revised": "revised" in s, "open": False}
            self.entries.append(self.collecting)
            self._render(rebuild=True)
            return
        if self.collecting is not None and re.match(r"^\d+\. \w+: ", s):
            self.collecting["steps"].append(s)
            self._render(rebuild=True)
            return
        self.collecting = None
        if friendly(line) is None:
            return
        self.entries.append({"type": "row", "line": line})
        self._render(rebuild=False)

    @objc.python_method
    def _entry_text(self, i: int, e: dict):
        if e["type"] == "row":
            return friendly(e["line"])
        if e["type"] == "answer":
            out = NSMutableAttributedString.alloc().initWithAttributedString_(
                styled(e["text"].strip() or "…", 13.5, 0, NSColor.labelColor(), space_before=4, indent=2, hang=False))
            if e["sources"]:
                line = NSMutableAttributedString.alloc().initWithAttributedString_(
                    styled("", 11.5, 0, NSColor.tertiaryLabelColor(), space_before=2, indent=2))
                line.deleteCharactersInRange_((0, line.length()))
                for k, (title, url) in enumerate(e["sources"]):
                    piece = NSMutableAttributedString.alloc().initWithAttributedString_(
                        styled(("  ·  " if k else "↗  ") + title, 11.5, 0, NSColor.secondaryLabelColor(), indent=2))
                    piece.deleteCharactersInRange_((piece.length() - 1, 1))   # no newline between sources
                    start = 5 if k else 3
                    piece.addAttribute_value_range_(NSLinkAttributeName, url, (start, piece.length() - start))
                    line.appendAttributedString_(piece)
                line.appendAttributedString_(NSAttributedString.alloc().initWithString_("\n"))
                out.appendAttributedString_(line)
            for query, url in e.get("suggestions") or []:   # Google Search suggestions, shown unchanged
                chip = NSMutableAttributedString.alloc().initWithAttributedString_(
                    styled(f"Google  {query}", 11.5, 0, NSColor.secondaryLabelColor(), indent=2, hang=False))
                chip.addAttribute_value_range_(NSLinkAttributeName, url, (0, chip.length() - 1))
                out.appendAttributedString_(chip)
            return out
        n = len(e["steps"])
        title = ("Revised plan" if e["revised"] else "Plan") + f" · {n} step{'s' if n != 1 else ''}"
        out = NSMutableAttributedString.alloc().initWithAttributedString_(
            styled(f"{title}  {'⌄' if e['open'] else '›'}", 11.5, 0.5, NSColor.secondaryLabelColor(),
                   space_before=4, indent=2))
        out.addAttribute_value_range_(NSLinkAttributeName, f"agentp-plan:{i}", (0, out.length() - 1))
        if e["open"]:
            for step in e["steps"]:
                out.appendAttributedString_(styled(plan_step_text(step), 12, 0, NSColor.secondaryLabelColor(),
                                                   indent=16))
        return out

    @objc.python_method
    def _render(self, rebuild: bool):
        storage = self.log.textStorage()
        if rebuild:
            storage.setAttributedString_(NSAttributedString.alloc().initWithString_(""))
            for i, e in enumerate(self.entries):
                t = self._entry_text(i, e)
                if t is not None:
                    storage.appendAttributedString_(t)
        else:
            t = self._entry_text(len(self.entries) - 1, self.entries[-1])
            if t is not None:
                storage.appendAttributedString_(t)
        self.log.scrollRangeToVisible_((storage.length(), 0))

    @objc.python_method
    def answer_delta(self, delta: str):
        """An answer arriving piece by piece: grow the last answer entry."""
        self.collecting = None
        if not self.entries or self.entries[-1]["type"] != "answer" or self.entries[-1]["done"]:
            self.entries.append({"type": "answer", "text": "", "sources": [], "done": False})
        self.entries[-1]["text"] += delta
        self._render(rebuild=True)

    @objc.python_method
    def answer_done(self, sources: list, suggestions: list = ()):
        if self.entries and self.entries[-1]["type"] == "answer":
            self.entries[-1]["sources"] = list(sources)
            self.entries[-1]["suggestions"] = list(suggestions)
            self.entries[-1]["done"] = True
            self._render(rebuild=True)

    def textView_clickedOnLink_atIndex_(self, view, link, index):
        link = str(link)
        if link.startswith("agentp-plan:"):
            e = self.entries[int(link.split(":")[1])]
            e["open"] = not e["open"]
            self._render(rebuild=True)
            return True
        return False

    @objc.python_method
    def show(self):
        front = agent.frontmost_name()
        if front and front != self.ctx.own_app:
            self.ctx.active_app = front     # the agent starts from the current frontmost app
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.field)

    @objc.python_method
    def hide(self):
        self.panel.orderOut_(None)

    @objc.python_method
    def toggle(self):
        if self.panel.isVisible() and self.panel.isKeyWindow():
            self.hide()
        else:
            self.show()

    # ------------------------------------------------ inline question
    @objc.python_method
    def ask(self, question: str, buttons: list[str], default: int, cancel: int, callback):
        """Show a question with buttons at the bottom; Return = default, Esc = cancel."""
        for v in list(self.prompt.subviews()):
            v.removeFromSuperview()
        self.answer_cb = callback
        self.panel.lets_go_of_keyboard = False
        if len(buttons) > 2:
            self._ask_list(question, buttons, default, cancel)
            return
        bar_h = 58
        sep = NSBox.alloc().initWithFrame_(NSMakeRect(0, bar_h - 1, W, 1))
        sep.setBoxType_(2)
        self.prompt.addSubview_(sep)

        x = W - PAD
        for i in range(len(buttons) - 1, -1, -1):   # right to left: the default ends up rightmost
            b = NSButton.buttonWithTitle_target_action_(buttons[i], self, "answer:")
            b.setTag_(i)
            b.setBezelStyle_(1)   # rounded
            if i == default:
                b.setKeyEquivalent_("\r")
            elif i == cancel:
                b.setKeyEquivalent_("\x1b")
            b.sizeToFit()
            bw = max(b.frame().size.width + 8, 76)
            x -= bw
            b.setFrame_(NSMakeRect(x, 14, bw, 30))
            self.prompt.addSubview_(b)
            x -= 8

        label = NSTextField.labelWithString_(question)
        label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.4))
        label.setTextColor_(NSColor.labelColor())
        label.setLineBreakMode_(4)   # truncate tail
        label.setFrame_(NSMakeRect(PAD, 19, max(x - PAD - 8, 80), 20))
        self.prompt.addSubview_(label)

        self.prompt.setFrame_(NSMakeRect(0, 0, W, bar_h))
        self.prompt.setHidden_(False)
        self.scroll.setFrame_(NSMakeRect(PAD - 4, bar_h + 6, W - PAD * 2 + 8, H - INPUT_H - bar_h - 12))
        self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def _ask_list(self, question: str, buttons: list[str], default: int, cancel: int):
        """'Which one?': question, then one full-width option per row, Cancel at the bottom right."""
        options = [i for i in range(len(buttons)) if i != cancel]
        row_h, top = 34, 40
        bar_h = top + row_h * len(options) + 48
        sep = NSBox.alloc().initWithFrame_(NSMakeRect(0, bar_h - 1, W, 1))
        sep.setBoxType_(2)
        self.prompt.addSubview_(sep)
        label = NSTextField.labelWithString_(question)
        label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.4))
        label.setFrame_(NSMakeRect(PAD, bar_h - 32, W - PAD * 2, 20))
        self.prompt.addSubview_(label)
        y = bar_h - top - row_h + 2
        for i in options:
            b = NSButton.buttonWithTitle_target_action_(buttons[i], self, "answer:")
            b.setTag_(i)
            b.setBezelStyle_(1)
            b.setAlignment_(0)        # left
            b.cell().setLineBreakMode_(4)
            if i == default:
                b.setKeyEquivalent_("\r")
            b.setFrame_(NSMakeRect(PAD - 6, y, W - PAD * 2 + 12, 30))
            self.prompt.addSubview_(b)
            y -= row_h
        c = NSButton.buttonWithTitle_target_action_(buttons[cancel], self, "answer:")
        c.setTag_(cancel)
        c.setBezelStyle_(1)
        c.setKeyEquivalent_("\x1b")
        c.sizeToFit()
        cw = max(c.frame().size.width + 8, 76)
        c.setFrame_(NSMakeRect(W - PAD - cw, 10, cw, 30))
        self.prompt.addSubview_(c)
        self.prompt.setFrame_(NSMakeRect(0, 0, W, bar_h))
        self.prompt.setHidden_(False)
        self.scroll.setFrame_(NSMakeRect(PAD - 4, bar_h + 6, W - PAD * 2 + 8, max(H - INPUT_H - bar_h - 12, 40)))
        self.panel.makeKeyAndOrderFront_(None)

    def answer_(self, sender):
        cb, self.answer_cb = self.answer_cb, None
        self.panel.lets_go_of_keyboard = self.busy
        if self.busy:
            self.panel.resignKeyWindow()
        self.prompt.setHidden_(True)
        self.scroll.setFrame_(NSMakeRect(PAD - 4, 12, W - PAD * 2 + 8, H - INPUT_H - 18))
        if cb:
            cb(int(sender.tag()))

    # ------------------------------------------------ command
    def submit_(self, sender):
        cmd = str(self.field.stringValue()).strip()
        if not cmd or self.busy:
            return
        self.field.setStringValue_("")
        self.append(f"› {cmd}")
        if cmd.casefold() in ("q", "quit", "exit"):
            AppHelper.stopEventLoop()
            return
        if agent.special_command(cmd, self.ctx):
            return
        if self.client is None:
            self.show_keys()
            return
        self._set_busy(True)
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    @objc.python_method
    def _run(self, cmd: str):
        try:
            if qa.TEST_REQUEST.search(cmd):   # "test example.com", "bu sayfayı test et", "test the login page of x.com"
                allow = self.ctx.io.confirm("Fill forms with made-up test data and submit them? "
                                            "(never pays, deletes or contacts anyone)", default_yes=True)
                r = qa.run_request(cmd, self.client, self.ctx.io, allow, self.ctx.planner)
                if r.get("report"):
                    subprocess.run(["open", r["report"]["html"]], check=False)
                return
            agent.handle(self.client, cmd, self.ctx)
        except Exception as e:
            self.ctx.io.say(f"✗ {e}")
        finally:
            AppHelper.callAfter(self._set_busy, False)

    @objc.python_method
    def _set_busy(self, busy: bool):
        self.busy = busy
        self.field.setEditable_(not busy)
        self.mic.setHidden_(busy)
        self.panel.lets_go_of_keyboard = busy
        if busy:
            self.spinner.startAnimation_(None)
            self.panel.resignKeyWindow()   # keys typed by the agent must reach the target app
        else:
            self.spinner.stopAnimation_(None)
            self.panel.makeKeyAndOrderFront_(None)
            self.panel.makeFirstResponder_(self.field)

    # ------------------------------------------------ menu bar + hotkeys
    @objc.python_method
    def _build_status_item(self):
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status.button().setTitle_("P")
        self.status.button().setFont_(NSFont.systemFontOfSize_weight_(13, 0.6))
        menu = NSMenu.alloc().init()
        for title, sel in (("Show AgentP    ⌥Space", "menuShow:"), ("Mark Last Request as Wrong", "menuWrong:"),
                           ("Clear Context", "menuReset:"), ("API Keys…", "menuKeys:"), (None, None),
                           ("Quit", "menuQuit:")):
            if title is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
            item.setTarget_(self)
            menu.addItem_(item)
        self.status.setMenu_(menu)

    def menuShow_(self, _):
        self.show()

    def menuWrong_(self, _):
        agent.mark_wrong(self.ctx)

    def menuReset_(self, _):
        self.entries, self.collecting = [], None
        self.log.setString_("")
        agent.special_command("new", self.ctx)

    def menuKeys_(self, _):
        self.show_keys()

    # ------------------------------------------------ API keys (the user's own, stored on this Mac)
    @objc.python_method
    def show_keys(self):
        if getattr(self, "keys_view", None) is not None:
            self.keys_view.removeFromSuperview()
        top = H - INPUT_H
        view = Sheet.alloc().initWithFrame_(NSMakeRect(1, 1, W - 2, top - 1))
        y = top - 44

        def label(text, size, weight, color, y, h=20):
            f = NSTextField.labelWithString_(text)
            f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
            f.setTextColor_(color)
            f.setFrame_(NSMakeRect(PAD, y, W - PAD * 2, h))
            view.addSubview_(f)
            return f

        label("Your API keys", 15, 0.6, NSColor.labelColor(), y)
        intro = label("Paste both keys and press Save. They stay on this Mac only.",
                      12, 0, NSColor.secondaryLabelColor(), y - 38, 34)
        intro.setLineBreakMode_(0)
        intro.cell().setWraps_(True)
        y -= 70
        self.key_fields = {}
        for name, (title, required, url) in config.KEYS.items():
            label(title, 12, 0.5, NSColor.labelColor(), y)
            field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(PAD, y - 32, W - PAD * 2 - 110, 26))
            field.setPlaceholderString_(config.mask(os.environ.get(name)) if os.environ.get(name) else "Paste key")
            view.addSubview_(field)
            get = NSButton.buttonWithTitle_target_action_("Get a key", self, "openKeyPage:")
            get.setBezelStyle_(1)
            get.setToolTip_(url)
            get.setTag_(list(config.KEYS).index(name))
            get.setFrame_(NSMakeRect(W - PAD - 100, y - 33, 100, 28))
            view.addSubview_(get)
            self.key_fields[name] = field
            y -= 66
        save = NSButton.buttonWithTitle_target_action_("Save", self, "saveKeys:")
        save.setBezelStyle_(1)
        save.setKeyEquivalent_("\r")
        save.setFrame_(NSMakeRect(W - PAD - 90, 16, 90, 30))
        view.addSubview_(save)
        if not config.missing_required():
            close = NSButton.buttonWithTitle_target_action_("Cancel", self, "closeKeys:")
            close.setBezelStyle_(1)
            close.setKeyEquivalent_("\x1b")
            close.setFrame_(NSMakeRect(W - PAD - 190, 16, 90, 30))
            view.addSubview_(close)
        self.root.addSubview_(view)
        self.keys_view = view
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.key_fields["TYPESAFE_API_KEY"])

    def openKeyPage_(self, sender):
        from AppKit import NSURL, NSWorkspace
        url = list(config.KEYS.values())[int(sender.tag())][2]
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    def saveKeys_(self, _):
        values = {}
        for name, field in self.key_fields.items():
            typed = str(field.stringValue()).strip()
            values[name] = typed or os.environ.get(name, "")   # empty field keeps the current key
        config.save(values)
        if config.missing_required():
            for name in config.missing_required():
                self.key_fields[name].setPlaceholderString_("Required: paste this key")
            return
        self.client = TypeSafeClient()
        self.ctx.planner = agent.llm.Gemini() if agent.llm and agent.llm.available() else None
        self.closeKeys_(None)
        planner = f"planner: Gemini ({self.ctx.planner.model})" if self.ctx.planner else "planner: off"
        self.append(f"(keys saved · {planner})")

    def closeKeys_(self, _):
        if getattr(self, "keys_view", None) is not None:
            self.keys_view.removeFromSuperview()
            self.keys_view = None
        self.panel.makeFirstResponder_(self.field)

    def menuQuit_(self, _):
        AppHelper.stopEventLoop()

    @objc.python_method
    def _install_hotkeys(self):
        def is_hotkey(e):
            flags = e.modifierFlags()
            return (e.keyCode() == KEY_SPACE and flags & NSEventModifierFlagOption
                    and not flags & (NSEventModifierFlagCommand | NSEventModifierFlagControl))

        def global_handler(e):
            if is_hotkey(e):
                AppHelper.callAfter(self.show)

        def local_handler(e):
            if is_hotkey(e):
                self.toggle()
                return None
            if e.keyCode() == KEY_ESC and self.answer_cb is None and not self.busy:
                self.hide()
                return None
            return e

        # the global monitor needs Accessibility permission (the terminal's permission is enough)
        self._g = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, global_handler)
        self._l = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, local_handler)


def main():
    agent.check_env(require_keys=False)   # missing keys are entered in the panel
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)   # no Dock icon
    ui = UI.alloc().init()
    if "--selftest" in sys.argv:   # build the panel, write from a background thread, quit
        threading.Thread(target=lambda: ui.ctx.io.say("› selftest"), daemon=True).start()
        AppHelper.callLater(0.5, lambda: (print(str(ui.log.string()).strip(), flush=True),
                                          AppHelper.stopEventLoop()))
    else:
        ui.show()
        from ApplicationServices import AXIsProcessTrusted
        if not AXIsProcessTrusted():
            ui.append("⚠ One permission needed: System Settings → Privacy & Security → Accessibility → turn on "
                      "Terminal. Then quit Terminal (⌘Q), reopen it and start AgentP again.")
        if ui.client is None:
            ui.show_keys()
    AppHelper.runEventLoop(installInterrupt=True)


if __name__ == "__main__":
    main()
