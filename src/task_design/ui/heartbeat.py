"""사람 시간 heartbeat: the browser reports visible-and-active intervals → ``record_activity``.

How it works
------------
- A ``st.components.v2`` component (JS runs in the app page, not an iframe) watches
  ``document.visibilityState`` and input events (mouse, keys, scroll, touch). The tab
  counts as *active* while it is visible and the last input was under ``IDLE_SECONDS``
  ago. Every ``BEAT_SECONDS`` while active it closes the current segment as a beat
  ``{seq, context, start, end}`` (ISO-8601 Z) and starts the next one at ``end``;
  going hidden or idle closes the segment at that moment (idle: at last input +
  ``IDLE_SECONDS``).
- Beats are sent to Python as a trigger value (``setTriggerValue``), which reruns
  the fragment the component lives in. Unacknowledged beats are resent with every
  send, and Python passes back the highest ``seq`` it has handled as ``data.acked``,
  so a beat lost between two reruns is delivered with the next one.
- ``context`` is the draft id open on screen when the beat was taken, or ``""`` on
  the 입력 폼 (no draft yet). ``HeartbeatLedger`` keeps those in the session and
  credits them to the draft the form creates.

Limitations (MVP)
-----------------
- A tab closed mid-segment loses up to one beat (≤ ``BEAT_SECONDS``); nothing is
  sent after the page is gone.
- Reading without touching mouse or keyboard for longer than ``IDLE_SECONDS`` counts
  as inactive.
- Time is credited to the staff member selected at the moment a beat arrives, and
  input-form time is lost if the form is abandoned or the session is reloaded
  before 시작.
- Two tabs on the same draft send overlapping intervals; metrics takes the union,
  so nothing is double counted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

BEAT_SECONDS = 15
IDLE_SECONDS = 60

JS = """
export default function (component) {
  const { data, setTriggerValue } = component;
  const BEAT_MS = (data && data.beat_ms) || 15000;
  const IDLE_MS = (data && data.idle_ms) || 60000;
  let hb = window.__tdHeartbeat;
  if (!hb) {
    hb = window.__tdHeartbeat = {
      send: null, context: "", segStart: null, lastInput: Date.now(),
      seq: Date.now(), pending: [],
    };
    const iso = (ms) => new Date(ms).toISOString();
    hb.visible = () => document.visibilityState === "visible";
    hb.push = (start, end) => {
      if (end - start < 1000) return;
      hb.seq += 1;
      hb.pending.push({ seq: hb.seq, context: hb.context, start: iso(start), end: iso(end) });
      hb.pending = hb.pending.slice(-50);
    };
    hb.flush = () => {
      if (hb.send && hb.pending.length) hb.send("beat", { beats: hb.pending });
    };
    hb.close = (at) => {
      if (hb.segStart !== null) { hb.push(hb.segStart, at); hb.segStart = null; }
    };
    hb.onInput = () => {
      hb.lastInput = Date.now();
      if (hb.segStart === null && hb.visible()) hb.segStart = hb.lastInput;
    };
    ["mousemove", "mousedown", "keydown", "wheel", "scroll", "touchstart", "input"]
      .forEach((ev) => document.addEventListener(ev, hb.onInput, { passive: true, capture: true }));
    document.addEventListener("visibilitychange", () => {
      if (hb.visible()) { hb.onInput(); } else { hb.close(Date.now()); hb.flush(); }
    });
    setInterval(() => {
      const now = Date.now();
      if (hb.segStart === null) return;
      if (!hb.visible()) { hb.close(now); }
      else if (now - hb.lastInput >= IDLE_MS) { hb.close(Math.min(now, hb.lastInput + IDLE_MS)); }
      else { hb.push(hb.segStart, now); hb.segStart = now; }
      hb.flush();
    }, BEAT_MS);
    if (hb.visible()) hb.segStart = Date.now();
  }
  const acked = (data && data.acked) || 0;
  hb.pending = hb.pending.filter((b) => b.seq > acked);
  const context = (data && data.context) || "";
  if (context !== hb.context) {
    // Screen switched to another draft: close what was measured for the old one.
    const now = Date.now();
    if (hb.segStart !== null) { hb.push(hb.segStart, now); hb.segStart = now; }
    hb.context = context;
  }
  hb.send = setTriggerValue;
  return () => { if (hb.send === setTriggerValue) hb.send = null; };
}
"""


@dataclass
class HeartbeatLedger:
    """Per-session bookkeeping: what was acknowledged, and 입력 폼 time not yet credited."""

    acked: int = 0
    pending: list[dict[str, str]] = field(default_factory=list)

    def receive(self, service: Any, *, staff_id: int, payload: Any) -> int:
        """Record the beats in ``payload`` (once each); returns the seq to acknowledge."""
        beats = payload.get("beats", []) if isinstance(payload, dict) else []
        for beat in sorted(beats, key=lambda b: b.get("seq", 0) if isinstance(b, dict) else 0):
            if not isinstance(beat, dict):
                continue
            seq = beat.get("seq")
            if not isinstance(seq, int) or seq <= self.acked:
                continue
            self.acked = seq
            interval = {"start": beat.get("start"), "end": beat.get("end")}
            context = beat.get("context") or ""
            if not context:
                self.pending.append(interval)
                continue
            self._record(service, context, staff_id, interval)
        return self.acked

    def credit_pending(self, service: Any, draft_id: UUID, *, staff_id: int) -> None:
        """Credit 입력 폼 time measured before the draft existed to ``draft_id``."""
        pending, self.pending = self.pending, []
        for interval in pending:
            self._record(service, str(draft_id), staff_id, interval)

    @staticmethod
    def _record(service: Any, context: str, staff_id: int, interval: dict) -> None:
        try:
            service.record_activity(UUID(context), staff_id, interval)
        except Exception:  # a bad beat must never break the page
            log.warning("dropped heartbeat for %s: %s", context, interval, exc_info=True)


_component = None


def render(service: Any, *, ledger: HeartbeatLedger, staff_id: int,
           draft_id: UUID | None) -> None:
    """Mount the heartbeat (invisible). Call inside an ``st.fragment`` so a beat only
    reruns that fragment."""
    import streamlit as st

    global _component
    if _component is None:
        _component = st.components.v2.component("td_heartbeat", js=JS)
    result = _component(
        key="td-heartbeat",
        data={"acked": ledger.acked, "context": str(draft_id) if draft_id else "",
              "beat_ms": BEAT_SECONDS * 1000, "idle_ms": IDLE_SECONDS * 1000},
        on_beat_change=lambda: None,
        height=0,
    )
    payload = getattr(result, "beat", None)
    if payload:
        ledger.receive(service, staff_id=staff_id, payload=payload)
