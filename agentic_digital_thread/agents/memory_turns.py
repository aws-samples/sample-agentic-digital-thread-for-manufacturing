# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structured blocks of a stored chat turn, for the Recent sessions reload.

The Strands session manager persists every message of a turn: the user's
question, each assistant message (narration text plus ``toolUse`` blocks)
and each tool-result message (``toolResult`` blocks). The display text built
in ``agents.memory_client`` flattens the tool parts to ``Tool call: <name>`` /
``Tool result`` labels, which leaves the chat nothing to rebuild a reopened
session's Reasoning trace or numbered Sources from. These helpers keep the
blocks, in order, so ``GET /api/memory/events`` can hand the chat the same
pieces a live answer streams: text, tool calls with their input, and tool
results summarized by the SAME one-line summarizer the live trace uses
(``agents.tool_result_summary``; the raw result rows never leave the server).

Never raises: a block that cannot be read is skipped.

A seeded question also carries the Knowledge Graph step the live trace showed
before the turn's first call (``ShownEvent.prelude``). That step is streamed by
the runtime, never stored as a message, so it is rebuilt from the seed the
stored user text starts with (``route_seed_trace.stored_step``); the seed
itself is never sent.

A turn the response-integrity gate ran for shows only what its gated stream
showed: tool names and ids, and metadata-only result summaries. The gate's
rendered answer is not stored, so such a turn reloads with no answer text
rather than the model's hidden draft.

A tool's charts are streamed by the runtime, never written into a message the
model sees, but the stored tool result keeps their tags (the session manager
stores it before ``ChartRedactionHook`` redacts the live copy). The app keeps
a record of which of them each turn's stream sent the browser
(``turn_evidence_store``), so an ungated tool result carries ``charts``: the
stored tags the live page got (:func:`with_reload_charts`), and a turn whose
charts the reload cannot all show is marked so the chat says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .internal_lines import public_text
from .route_seed_trace import stored_step
from .tool_result_summary import summarize_tool_result

# A tool input larger than this (as JSON) is sent as a truncated string: the
# trace shows a short summary of the input, never the whole of a big one.
_MAX_INPUT_CHARS = 4000

#: The hard bound on one reload response's chart strings: the bytes their
#: JSON strings (and separators) add to the response body, across all its
#: turns. A chart drawn at the i3X point cap (150 points per series) is about
#: 5 KB of tag text for one series and about 9 KB for four, and a SiteWise read
#: draws at most 8 charts, so this keeps on the order of a hundred charts. It
#: is spent newest turn first; an older turn whose charts no longer fit is
#: marked instead (:func:`with_reload_charts`).
MAX_RELOAD_CHART_BYTES = 1024 * 1024

#: The tool Strands registers for ``structured_output_model=FinalAnswerDraft``
#: (the response-integrity gate's typed answer draft). Its call is the gate's
#: own, never announced live; its presence marks a turn the gate ran for.
STRUCTURED_ANSWER_TOOL = "FinalAnswerDraft"


def _bounded_input(value: Any) -> Any:
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        return str(value)[:_MAX_INPUT_CHARS]
    return value if len(text) <= _MAX_INPUT_CHARS else text[:_MAX_INPUT_CHARS]


def message_blocks(raw: str) -> Optional[list[dict]]:
    """Ordered blocks of one stored message envelope, or ``None`` when ``raw``
    is not an envelope (a plain-text turn).

    Block shapes: ``{"type": "text", "text"}``,
    ``{"type": "tool_use", "tool_id", "tool", "input"}`` and
    ``{"type": "tool_result", "tool_id", "status", "result"}`` (the raw
    result, kept only until ``display_blocks`` summarizes it). Reasoning and
    other non-display blocks are left out, so a reasoning-only message gives
    ``[]``."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    msg = obj.get("message") if isinstance(obj, dict) else None
    inner = msg if isinstance(msg, dict) else obj
    content = inner.get("content") if isinstance(inner, dict) else inner
    if not isinstance(content, list):
        return None
    blocks: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        text = b.get("text")
        tool_use = b.get("toolUse")
        tool_result = b.get("toolResult")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        elif isinstance(tool_use, dict):
            blocks.append({
                "type": "tool_use",
                "tool_id": str(tool_use.get("toolUseId") or ""),
                "tool": str(tool_use.get("name") or ""),
                "input": _bounded_input(tool_use.get("input", {})),
            })
        elif isinstance(tool_result, dict):
            blocks.append({
                "type": "tool_result",
                "tool_id": str(tool_result.get("toolUseId") or ""),
                "status": str(tool_result.get("status") or ""),
                "result": tool_result,
            })
    return blocks


def _summary(tool: str, result: Any) -> str:
    try:
        return summarize_tool_result(tool, result)
    except Exception:  # noqa: BLE001 - a summary is display-only, never fatal
        return ""


@dataclass(frozen=True)
class ShownEvent:
    """What the reload shows for one stored event."""

    blocks: list
    #: Display text replacing the event's own content: in a gated turn, the
    #: ``Tool call: <name>`` / ``Tool result`` labels only; for an assistant
    #: event, its content without an orchestrator-only engine line. None keeps it.
    label: Optional[str] = None
    #: For a seeded question: the Knowledge Graph step's call and result, as
    #: the live trace showed them before the turn's first stored call
    #: (:func:`seed_step_blocks`). Empty for every other event.
    prelude: list = field(default_factory=list)


def _is_question(e) -> bool:
    blocks = getattr(e, "blocks", None) or []
    content = getattr(e, "content", "") or ""
    # ``not content.isspace()`` is ``bool(content.strip())`` without a copy.
    return (e.role == "user" and bool(content) and not content.isspace()
            and not any(b.get("type") == "tool_result" for b in blocks))


#: The tool name the live prelude announces the Knowledge Graph step under
#: (``runtime_app._seed_trace_events``).
SEED_STEP_TOOL = "explore_graph"


def seed_step_blocks(event, *, gated: bool) -> list[dict]:
    """A seeded question's Knowledge Graph step, as the live trace showed it:
    a ``tool_use`` and a ``tool_result`` block, rebuilt from the seed the
    stored user text starts with (``route_seed_trace.stored_step``). ``[]``
    for any other event, a turn without a seed, or a seed that does not parse.

    Ungated: the call's input (its label) and the result summary the live
    trace gave it (:func:`_live_step_summary`). Gated (``gated``): the tool
    name and id and the metadata-only summary the gated stream gave this
    result (:func:`_gated_stream_summary`). The stored text is read only as
    far as ``route_seed_trace.stored_turn_read_chars`` and never copied
    whole."""
    step = stored_step(getattr(event, "content", "")) if _is_question(event) else None
    if step is None:
        return []
    tool_id = step["tool_id"]
    result = {"toolUseId": tool_id, "status": "success", "content": [{"text": step["text"]}]}
    call = {"type": "tool_use", "tool_id": tool_id, "tool": SEED_STEP_TOOL}
    done = {"type": "tool_result", "tool_id": tool_id, "tool": SEED_STEP_TOOL}
    if gated:
        return [call, {**done, "summary": _gated_stream_summary(result)}]
    return [{**call, "input": step["input"]},
            {**done, "status": "success", "summary": _live_step_summary(result)}]


def _live_step_summary(result: dict) -> str:
    """The summary the ungated live stream gives a tool result the runtime
    forwards whole, as the Knowledge Graph step's is
    (``chat_client._tool_result_from_payload``): the shared summarizer, which
    drops orchestrator-only engine lines, with graph-known ids named
    (``id_name_resolver.annotate_ids_in_text``)."""
    try:
        from .chat_client import _tool_result_from_payload
        return _tool_result_from_payload(result).summary
    except Exception:  # noqa: BLE001 - a summary is display-only, never fatal
        return _summary("", result)


def _gated_stream_summary(result: dict) -> str:
    """The summary the gated live stream gives a tool result the runtime
    forwards whole, as the Knowledge Graph step's is: its metadata only
    (``chat_client._CloudStreamState._result_events``)."""
    from .tool_result_capture import safe_tool_result_metadata
    try:
        metadata = safe_tool_result_metadata(result, tool_name=SEED_STEP_TOOL, max_chars=1024)
        return json.dumps(metadata, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - a summary is display-only, never fatal
        return "{}"


def _gated_turns(events: list, enforced: bool) -> list[bool]:
    """Per event: does it belong to a turn the response-integrity gate ran
    for? Every turn when the gate is on now; otherwise a turn whose stored
    messages include the gate's structured-answer call, so switching the gate
    off later never exposes what it hid. A turn runs from one question to the
    next (events before the first question form a turn of their own)."""
    turn_of: list[int] = []
    turn = 0
    for e in events:
        if _is_question(e):
            turn += 1
        turn_of.append(turn)
    marked = {
        turn_of[i] for i, e in enumerate(events)
        for b in getattr(e, "blocks", None) or []
        if b.get("type") == "tool_use" and b.get("tool") == STRUCTURED_ANSWER_TOOL
    }
    return [enforced or turn_of[i] in marked for i in range(len(events))]


def question_turns(events: list, shown: list, *, enforced: bool = False
                   ) -> dict[int, tuple[str, bool]]:
    """Per question (its index in ``events``): the turn's identity
    (``turn_evidence_store.turn_identity``) and whether the reload shows the
    turn gated (:func:`_gated_turns`). The identity is read from what the
    reload replays, as the live stream showed it: the stored user text (read
    by ``turn_evidence_store.turn_question``, the one form both sides hash)
    and the ids of the calls in ``shown``, the
    views :func:`display_blocks` built (a gated turn's in their public form,
    without the gate's own call). Events before the first question belong to
    no question."""
    from .turn_evidence_store import turn_identity
    gated = _gated_turns(events, enforced)
    turns: dict[int, tuple[str, list]] = {}
    current = None
    for i, (e, view) in enumerate(zip(events, shown)):
        if _is_question(e):
            current = i
            turns[i] = (e.content, [])
        elif current is not None:
            turns[current][1].extend(b["tool_id"] for b in view.blocks
                                     if b.get("type") == "tool_use" and b.get("tool_id"))
    return {i: (turn_identity(question, ids), gated[i]) for i, (question, ids) in turns.items()}


def _public_call(tool: str, tool_id: str) -> Optional[tuple[str, str]]:
    """(public tool id, tool name) as the gated stream announces the call, or
    None when it announces nothing for it (an unsafe name, the gate's own
    structured-answer call)."""
    from .runtime_integrity import safe_tool_progress
    if tool == STRUCTURED_ANSWER_TOOL:
        return None
    progress = safe_tool_progress({"current_tool_use": {"name": tool, "toolUseId": tool_id}})
    if progress is None:
        return None
    current = progress["current_tool_use"]
    return current["toolUseId"], current["name"]


def _public_result_summary(tool: str, result: Any) -> str:
    """The gated stream's tool_result summary: the runtime's metadata-only
    result event, serialized as the chat client sends it (no rows, values or
    identifiers)."""
    from .runtime_integrity import public_tool_result_event
    from .tool_result_capture import safe_tool_result_metadata
    try:
        metadata = safe_tool_result_metadata(result, tool_name=tool, max_chars=1024)
        event = public_tool_result_event(tool_use_id="", tool_name=tool, metadata=metadata)
        return json.dumps(event["tool_result_metadata"]["metadata"], separators=(",", ":"))
    except Exception:  # noqa: BLE001 - a summary is display-only, never fatal
        return "{}"


def _gated_event(e, names: dict[str, str]) -> ShownEvent:
    """Only what the gated stream showed: each call's tool name and id (no
    input) and each result's metadata-only summary. Never text: the model's
    narration and answer are hidden live, and the gate's rendered answer is
    not stored. A seeded question keeps the gated form of its Knowledge Graph
    step (:func:`seed_step_blocks`)."""
    if _is_question(e):
        return ShownEvent(blocks=[], prelude=seed_step_blocks(e, gated=True))
    shown: list[dict] = []
    for b in getattr(e, "blocks", None) or []:
        if b.get("type") == "tool_use":
            call = _public_call(b.get("tool", ""), b.get("tool_id", ""))
            if call:
                shown.append({"type": "tool_use", "tool_id": call[0], "tool": call[1]})
        elif b.get("type") == "tool_result":
            tool = names.get(b.get("tool_id", ""), "")
            call = _public_call(tool, b.get("tool_id", "")) if tool else None
            if call:
                shown.append({"type": "tool_result", "tool_id": call[0], "tool": call[1],
                              "summary": _public_result_summary(tool, b.get("result"))})
    labels = [f"Tool call: {b['tool']}" if b["type"] == "tool_use" else "Tool result"
              for b in shown]
    return ShownEvent(blocks=shown, label="\n".join(labels))


def display_blocks(events: list, *, enforced: bool = False) -> list[ShownEvent]:
    """Per event (same order), the JSON-ready blocks the chat replays.

    * A tool result gets the name of the call it answers (matched by
      toolUseId across the whole session) and that tool's one-line summary.
    * A user event keeps ONLY its tool results: its text is the composed
      route seed + preambles + question, and the question is already the
      event's display ``content``. A seeded question also gets ``prelude``:
      the Knowledge Graph step the live trace showed (:func:`seed_step_blocks`).
    * An assistant event that is not an envelope is one text block.
    * An assistant event's text drops the engine lines meant for the
      orchestrator only (``internal_lines.public_text``), in its blocks and,
      through ``label``, its display content: a record stored before the
      session store dropped them can still hold one.
    * A turn the response-integrity gate ran for (``enforced``, the live
      chat's own decision, or a stored structured-answer call) shows only
      what the gated stream showed (``_gated_event``).
    * No tool result carries ``charts`` here: :func:`with_reload_charts`
      adds the ones the turn's stream sent, from the kept delivery record."""
    names: dict[str, str] = {}
    for e in events:
        for b in getattr(e, "blocks", None) or []:
            if b.get("type") == "tool_use" and b.get("tool_id"):
                names[b["tool_id"]] = b.get("tool", "")
    gated = _gated_turns(events, enforced)
    return [_gated_event(e, names) if gated[i] else _ungated_event(e, names)
            for i, e in enumerate(events)]


def _ungated_event(e, names: dict[str, str]) -> ShownEvent:
    """One event of a turn the gate did not run for (:func:`display_blocks`)."""
    blocks = getattr(e, "blocks", None)
    content = getattr(e, "content", "") or ""
    if blocks is None:
        blocks = [{"type": "text", "text": content}] if e.role != "user" and content else []
    shown: list[dict] = []
    for b in blocks:
        if b.get("type") == "tool_result":
            tool = names.get(b.get("tool_id", ""), "")
            shown.append({
                "type": "tool_result",
                "tool_id": b.get("tool_id", ""),
                "tool": tool,
                "status": b.get("status", ""),
                "summary": _summary(tool, b.get("result")),
            })
        elif e.role != "user":
            shown.append(_public_block(b))
    return ShownEvent(blocks=shown, label=_public_label(e.role, content),
                      prelude=seed_step_blocks(e, gated=False))


def with_reload_charts(shown: list, events: list, turns: dict, records: dict, *,
                       ceiling: int = MAX_RELOAD_CHART_BYTES) -> "tuple[list, set]":
    """``shown`` with each ungated tool result carrying ``charts``: the chart
    tags its stored result holds (``orchestrator.chart_tags_in``, deduped
    across the turn by the delivery key) that the turn's stream sent the
    browser, and the questions whose answer is marked as not showing all its
    charts. ``turns`` is :func:`question_turns`; ``records`` maps a question
    to the kept record of its stream (``turn_evidence_store.kept_for_turns``:
    ``{"fingerprints", "cut"}``).

    Fail closed: a turn with no record, and a gated turn, replays no chart. A
    stored tag is replayed only when its fingerprint
    (``orchestrator.chart_fingerprint`` of the text the stream sends) is in
    the record. The ``ceiling`` bounds the response's chart strings, spent
    newest turn first, a tag that does not fit skipped (:func:`_fitting`).
    A question is marked when a rendered chart (not a notice-only tag, which
    says no chart was drawn) it may have shown live is not replayed: it has
    no record, the ceiling held one back, or the record was cut and a stored
    chart is not in it. Never when what it replays is what the record says
    the page got, and never for a gated turn, whose stream never mentioned
    charts. The given views are not changed."""
    stored = _stored_charts(events)
    sent: dict = {}
    marked: set = set()
    left = ceiling
    for question in sorted(stored, reverse=True):
        if turns.get(question, ("", True))[1]:
            continue
        tags = stored[question]
        record = records.get(question)
        wanted, unrecorded = _delivered(tags, record)
        taken, left, held = _fitting(wanted, left)
        for place, tag in taken:
            sent.setdefault(place, []).append(tag)
        missing = tags if record is None else [*held, *unrecorded]
        if any(not _is_notice(tag) for _, tag in missing):
            marked.add(question)
    return _with_charts(shown, sent), marked


def _is_notice(tag: str) -> bool:
    """Whether ``tag`` is notice-only: it says no chart was drawn."""
    from .orchestrator import _CHART_NOTICE_RE
    return bool(_CHART_NOTICE_RE.search(tag))


def _stored_charts(events: list) -> dict:
    """``{question index: [((event index, result index), tag), ...]}``: each
    turn's stored chart tags, in order, deduped across the turn by the key
    the live drain dedups on per request Agent (``orchestrator._chart_dedup_key``),
    as the text the stream sends (:func:`_as_streamed`). Events before the
    first question belong to no turn. Never raises."""
    from .orchestrator import _chart_dedup_key, chart_tags_in
    found: dict = {}
    question, seen = None, set()
    for i, e in enumerate(events):
        if _is_question(e):
            question, seen = i, set()
            continue
        results = [b for b in getattr(e, "blocks", None) or [] if b.get("type") == "tool_result"]
        for j, b in enumerate(results if question is not None else []):
            for tag in chart_tags_in(b.get("result")):
                key = _chart_dedup_key(tag)
                if key not in seen:
                    seen.add(key)
                    found.setdefault(question, []).append(((i, j), _as_streamed(tag)))
    return found


def _as_streamed(tag: str) -> str:
    """A tag as the browser gets it: the text of the stream's token event."""
    from .events import MessageEvent
    return MessageEvent(tag).content


def _delivered(tags: list, record: "dict | None") -> "tuple[list, list]":
    """(the tags the record says the stream sent, the tags a cut record may
    have left out). No record: none of them."""
    if not record:
        return [], []
    from .orchestrator import chart_fingerprint
    kept = set(record.get("fingerprints") or ())
    wanted = [(place, tag) for place, tag in tags if chart_fingerprint(tag) in kept]
    unrecorded = ([(place, tag) for place, tag in tags if chart_fingerprint(tag) not in kept]
                  if record.get("cut") else [])
    return wanted, unrecorded


def _fitting(tags: list, left: int) -> "tuple[list, int, list]":
    """(the tags that fit in ``left`` bytes, in order, the bytes left, the
    tags that did not fit). A tag's bytes are what its JSON string and
    separator add to the response; one that does not fit is skipped, later
    smaller ones still sent."""
    taken, held = [], []
    for place, tag in tags:
        size = len(json.dumps(tag, ensure_ascii=False).encode("utf-8")) + 1
        if size > left:
            held.append((place, tag))
            continue
        left -= size
        taken.append((place, tag))
    return taken, left, held


def _with_charts(shown: list, sent: dict) -> list:
    """``shown`` with ``sent[(event index, result index)]`` as the ``charts``
    of that event's tool result, counted in order; other views as given."""
    out = []
    for i, view in enumerate(shown):
        if not any(place[0] == i for place in sent):
            out.append(view)
            continue
        blocks, j = [], 0
        for b in view.blocks:
            if b.get("type") == "tool_result":
                tags = sent.get((i, j))
                b = {**b, "charts": tags} if tags else b
                j += 1
            blocks.append(b)
        out.append(replace(view, blocks=blocks))
    return out


def _public_block(block: dict) -> dict:
    """An assistant block as shown: a text block without the engine lines
    meant for the orchestrator only (``internal_lines.public_text``), which
    a record stored before the session store dropped them can still hold."""
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        return block
    text = public_text(block["text"])
    return block if text == block["text"] else {**block, "text": text}


def _public_label(role: str, content: str) -> Optional[str]:
    """The display text replacing an assistant event's content when that
    content holds an orchestrator-only engine line, else ``None``."""
    if role == "user" or not content:
        return None
    text = public_text(content)
    return None if text == content else text


def with_route_contexts(shown: list, lookup) -> list:
    """``shown`` with each system delegation's tool result carrying the route
    context its call was sent (``route_context``), read once for all of them
    with ``lookup(tool ids) -> {tool id: text}``. A gated event (one that
    has a display label) is left as it is; a call with no kept context gets
    no key. The given views are not changed, and each keeps its ``prelude``."""
    ids = [b["tool_id"] for view in shown if view.label is None for b in view.blocks
           if b.get("type") == "tool_result" and b.get("tool_id")
           and "___query_" in str(b.get("tool") or "")]
    if not ids:
        return shown
    try:
        found = lookup(ids) or {}
    except Exception:  # noqa: BLE001 - a reload without the context is the fallback
        found = {}
    if not found:
        return shown
    return [view if view.label is not None else replace(
        view, blocks=[{**b, "route_context": found[b["tool_id"]]}
                      if b.get("type") == "tool_result" and b.get("tool_id") in found else b
                      for b in view.blocks]) for view in shown]


def with_trace_telemetry(shown: list, events: list, lookup) -> list:
    """``shown`` with each system call's tool result carrying the telemetry
    lines its runtime stored for it: ``telemetry_coverage`` (shown above the
    summary) and ``sample_line`` (shown below it), read once for all of them
    with ``lookup(tool ids) -> {tool id: (coverage lines, sample line)}``
    (:func:`_with_stored_telemetry` says what each block keeps).
    Only a system query call is looked up, in :func:`_trace_read_order`, so
    a reload reads nothing for other tools; the lines shown are the stored
    ones, never the result's own, with graph-known ids named as the live
    trace names them (:func:`_named_trace_lines`). A gated event (one that
    has a display label) is left as it is. The given views are not changed,
    and each keeps its ``prelude``."""
    ids = _trace_read_order(shown, events)
    if not ids:
        return shown
    try:
        found = lookup(ids) or {}
    except Exception:  # noqa: BLE001 - a reload without the lines is the fallback
        found = {}
    found = _named_trace_lines(found)
    eligible = set(ids)
    return [view if view.label is not None else replace(
        view, blocks=[_with_stored_telemetry(b, found.get(b["tool_id"]))
                      if b.get("type") == "tool_result" and b.get("tool_id") in eligible else b
                      for b in view.blocks]) for view in shown]


def _named_trace_lines(found: dict) -> dict:
    """``found`` with the graph-known ids in every stored line named the way
    the live trace step named them: each call's lines through the same
    ``id_name_resolver.annotate_ids_in_lines`` the live step uses
    (``chat_client._trace_telemetry``), on their own, so a name is chosen per
    call exactly as live. The session's ids are first looked up together
    (``id_name_resolver.prefetch_names``), starting no lookup after
    :data:`RELOAD_NAME_BUDGET_S`; each call is then named from the names
    found. When the lookups finish inside the budget the reopened chat shows
    what the live chat showed; past it, the ids not yet named stay as stored.
    Fail-soft: lines no name is found for keep their text."""
    from . import id_name_resolver as resolver
    lines = [line for coverage, sample in found.values() for line in (*coverage, sample) if line]
    try:
        names = resolver.prefetch_names(resolver.extract_id_tokens("\n".join(lines)),
                                        budget_s=RELOAD_NAME_BUDGET_S)
    except Exception:  # noqa: BLE001 - naming is display-only, never fatal
        return found
    named = {}
    for tool_id, (coverage, sample) in found.items():
        out = resolver.annotate_ids_in_lines([*coverage, *([sample] if sample else [])],
                                             names=names)
        named[tool_id] = (out[:len(coverage)], out[len(coverage)] if sample else sample)
    return named


#: Time after which a reload starts no further name lookup (each lookup is
#: one short attempt, ``id_name_resolver.LOOKUP_TIMEOUT_MS``), so a slow graph
#: delays a reopened chat by at most this plus one lookup.
RELOAD_NAME_BUDGET_S = 10.0


def _trace_read_order(shown: list, events: list) -> list:
    """The ungated system query calls of ``shown``
    (``telemetry_coverage.may_hold_trace_telemetry``) in the order their
    entries are read: first those whose stored result names the sample line
    (``telemetry_coverage.names_sample_line``), then the others, which can
    hold only coverage lines. The store reads at most ``MAX_SAMPLE_READS``,
    so many earlier calls cannot keep a sampled one from being read. The
    result text only orders the reads; it is never shown."""
    from .telemetry_coverage import may_hold_trace_telemetry, names_sample_line
    eligible = [b["tool_id"] for view in shown if view.label is None for b in view.blocks
                if b.get("type") == "tool_result" and b.get("tool_id")
                and may_hold_trace_telemetry(b.get("tool"))]
    named = {b.get("tool_id") for e in events for b in getattr(e, "blocks", None) or []
             if b.get("type") == "tool_result" and names_sample_line(str(b.get("result")))}
    return [i for i in eligible if i in named] + [i for i in eligible if i not in named]


def _with_stored_telemetry(block: dict, stored) -> dict:
    """One system call's tool-result block: its summary without any copy of
    the sample line, whatever the read returned
    (``telemetry_coverage.system_call_summary``), and, when its entry was
    read (``stored``, its ``(coverage lines, sample line)``), the stored
    lines as their own fields."""
    from .telemetry_coverage import system_call_summary
    coverage, sample = stored or ((), "")
    block = {**block, "summary": system_call_summary(block.get("tool"), block.get("summary") or "")}
    if coverage:
        block["telemetry_coverage"] = list(coverage)
    if sample:
        block["sample_line"] = sample
    return block
