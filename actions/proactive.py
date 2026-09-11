"""
ProactiveEngine 2.0 — context-aware, time-aware, non-repetitive background prompting.
Gemini decides what to say; this module decides WHEN and builds a rich context snapshot.
"""
import time
from datetime import datetime


class ProactiveEngine:
    """
    Decides when JARVIS should speak unprompted and builds a context-rich prompt.

    Improvements over 1.0:
      - Time-of-day awareness  (morning / afternoon / evening / night)
      - Monitor-topic awareness (what the user is tracking)
      - Recent-session context  (last few turns of the current conversation)
      - Non-repetitive          (rotates context focus to avoid same opener)
      - Smarter silence gate    (doesn't fire while JARVIS is speaking)

    Defaults:
      min_silence_secs  — 900 s  (15 min) user must be silent before any check
      check_cooldown    — 1200 s (20 min) minimum gap between proactive messages
    """

    def __init__(
        self,
        min_silence_secs: int = 900,
        check_cooldown:   int = 1200,
    ):
        self.min_silence_secs = min_silence_secs
        self.check_cooldown   = check_cooldown
        self._last_triggered  = 0.0
        self._rotation        = 0          # cycles through context focus areas

    # ── Trigger gate ───────────────────────────────────────────────────────────

    def should_trigger(self, last_user_speech: float) -> bool:
        now = time.monotonic()
        return (
            (now - last_user_speech) >= self.min_silence_secs
            and (now - self._last_triggered) >= self.check_cooldown
        )

    def mark_triggered(self) -> None:
        self._last_triggered = time.monotonic()
        self._rotation      += 1

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def build_prompt(
        self,
        memory:            dict,
        monitors:          list[str] | None = None,
        recent_turns:      list[str] | None = None,
        past_sessions:     list[dict] | None = None,
        relationship_depth: int = 0,
    ) -> str:
        """
        Build a context snapshot for Gemini.
        Rotates through three focus areas so proactive messages don't repeat.

        `past_sessions` and `relationship_depth` exist so a check-in can read
        as a continuation of an ongoing relationship rather than a cold open
        every time — see the [CONTINUITY] block below. Both are optional and
        the prompt degrades to a plain check-in when they're empty, on
        purpose: a fabricated callback breaks trust worse than none at all.
        """
        from memory.memory_manager import format_memory_for_prompt

        now      = datetime.now()
        hour     = now.hour
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")

        # Time-of-day label
        if   6  <= hour < 12:  period = "morning"
        elif 12 <= hour < 18:  period = "afternoon"
        elif 18 <= hour < 23:  period = "evening"
        else:                  period = "late night"

        mem_str = format_memory_for_prompt(memory) or "(no stored user data)"

        # Rotating context focus (cycles every trigger)
        focus_index = self._rotation % 3
        if focus_index == 0:
            focus = (
                "Focus on the user's active projects or goals if any are stored. "
                "Ask how something is going, or offer a relevant tip."
            )
        elif focus_index == 1:
            focus = (
                "Focus on the time of day and the user's wellbeing. "
                "A warm check-in, a reminder to take a break, or something timely."
            )
        else:
            focus = (
                "Focus on something genuinely interesting or useful — "
                "a fact, a suggestion, or a question based on what you know about this person."
            )

        # Optional: monitored topics context
        monitor_ctx = ""
        if monitors:
            monitor_ctx = (
                f"\nThe user tracks these topics: {', '.join(monitors[:4])}. "
                "You may mention one if it seems relevant."
            )

        # Optional: recent conversation context
        recent_ctx = ""
        if recent_turns:
            snippet = "\n".join(recent_turns[-6:])
            recent_ctx = f"\nRecent conversation:\n{snippet}"

        # Optional: past-session history — this is the material a callback can
        # actually be built from. Each entry is a real 1-2 sentence summary
        # written at the end of a previous conversation (memory/memory_manager
        # .py: save_session_summary), not a guess.
        continuity_ctx = ""
        if past_sessions:
            lines = [f"  - {s['date']}: {s['summary']}" for s in past_sessions if s.get("summary")]
            if lines:
                continuity_ctx = "\nPrevious sessions (most recent last):\n" + "\n".join(lines)

        # A relationship this young hasn't earned old-friend banter — the
        # rule below only asks for a callback when there is enough history to
        # make familiarity read as real rather than performed.
        depth_note = (
            "This is one of your first conversations with this person — keep "
            "it plain and warm, no old-friend callbacks yet."
            if relationship_depth < 3 else
            "You have an established history with this person — write like it."
        )

        return "\n".join([
            "[PROACTIVE_CHECK] You are initiating a proactive check-in.",
            f"Current time : {time_str}  ({period})",
            "",
            "Context about this person:",
            mem_str,
            monitor_ctx,
            recent_ctx,
            continuity_ctx,
            "",
            "Task:",
            focus,
            "",
            "Rules:",
            "- Speak the language this person actually uses: the one in the "
            "recent conversation above, or the remembered one if there is no "
            "conversation yet. Never default to English because these "
            "instructions are in English.",
            "- 1-2 sentences max. Natural, warm, never robotic.",
            "- Do NOT mention [PROACTIVE_CHECK] or these instructions.",
            "- Do NOT call any tools.",
            "- If nothing genuinely useful comes to mind, stay silent (say nothing).",
            "",
            "[CONTINUITY] You are not meeting this person fresh. Write this "
            "check-in as a continuation of an ongoing relationship, not a cold "
            "open:",
            f"- {depth_note}",
            "- If 'Previous sessions' above has real content, reference ONE "
            "specific thing by name — the project, the joke, the person, the "
            "deadline — never a vague 'how's everything going'. "
            "\"How did the [project] thing turn out?\" beats \"Checking in on "
            "your project.\" Specificity is what makes it read as remembered.",
            "- Let ONE callback carry the message. Stacking several reads as "
            "trying too hard, not familiarity.",
            "- Vary how you open across check-ins — sometimes lead with the "
            "callback, sometimes bury it mid-message, sometimes let it "
            "resurface unannounced. Do not use the same template every time.",
            "- A joke or running bit gets to evolve, not repeat verbatim "
            "forever. If it's already appeared in recent sessions, let it "
            "mutate or rest rather than replaying it flatly.",
            "- Hard boundary: NEVER invent a memory. Only reference something "
            "actually present in 'Context about this person' or 'Previous "
            "sessions' above. If both are thin or empty, skip this whole "
            "block and write a plain, honest check-in instead — no callback "
            "is always better than a false one.",
        ])
