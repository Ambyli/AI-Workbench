"""Decision points: one per user message of every exported chat.

    python -m bench.points --chats DATA/chats.jsonl --out DATA/points.jsonl \\
        [--decisions DATA/decisions.jsonl] [--sample 200 --sample-out DATA/sample.jsonl]

A DECISION POINT is the moment route-policy's ``/decide`` ran for a user
message: the conversation up to and including that message, as ``/decide``
received it (``convert.replay``: Open WebUI's rebuild, then LiteLLM's footer
strip). For each one this records

* ``messages`` -- the OpenAI messages up to and including user message
  ``k`` (``k`` = how many user messages they contain, so
  ``render_transcript(messages, upto_user=k)`` and the rules see exactly what
  ``/decide`` saw; ``k`` can differ from ``user_index``, the message's place
  among the STORED user messages, when Open WebUI added a tool-image user
  message or compaction dropped earlier ones);
* ``gap_s`` -- seconds since the previous user message: the time a verdict
  requested at this message has before the next one needs it;
* **weak labels**, by precedence (``baseline.weak_label``):
  1. the privacy rule matches the user message -> ``local-private``;
  2. the ANSWER to it called a tool the ``code-tools`` regex matches ->
     ``local-code``;
  3. the primary decision logged for the answer (``decisions.jsonl``: the
     earliest row with this ``chat_id`` + ``message_id`` = the answer's id,
     ``shadow_of`` null) -> its ``chosen_route``;
  4. else none;
* ``rules_route`` -- what the live ``default`` ruleset decides for these
  messages, run offline (``baseline``): the agreement baseline;
* ``privacy_kw_route`` -- ``local-private`` iff the privacy rule matched: the
  baseline the classifier's ``local-private`` recall must reach.

``--sample N`` also writes a stratified sample for gold labelling: points
grouped by (weak label, rules route), taken round-robin across the groups in
a seeded random order -- so rare groups such as ``local-private`` are fully
represented before common ones fill the rest -- at most ``--sample-per-chat``
from one chat, written shuffled.

``--keep-exchanges N`` bounds the file: each point keeps only its last N
exchanges (``trimmed_exchanges`` says how many were cut; ``k`` counts the
kept ones). The labels and ``rules_route`` are computed on the full history
first, and a transcript is unchanged as long as N >= the criteria set's
``history.exchanges`` (``run.py`` warns otherwise). 0 (the default) keeps
everything.

The output is CUSTOMER DATA, like its input. Schema: BENCH.md § File formats.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections import defaultdict
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from common.conversation import called_tool_names, user_text

from . import convert
from .baseline import DEFAULT_RULESET, PRIVATE_ROUTE, RuleBaseline, weak_label
from .jsonl import dumps, read_jsonl, warn

DEFAULT_SAMPLE_SEED = 471
DEFAULT_SAMPLE_PER_CHAT = 5


# -- the primary decisions ----------------------------------------------------------


class DecisionIndex:
    """(chat_id, message_id) -> the chosen route of the EARLIEST primary
    decision for that answer (its first ``/decide``, made at the user message,
    before any tool round of the same answer)."""

    def __init__(self) -> None:
        self._by_answer: Dict[Tuple[str, str], Tuple[str, str]] = {}

    @classmethod
    def from_records(cls, records: Sequence[Dict[str, Any]]) -> "DecisionIndex":
        index = cls()
        for row in records:
            if row.get("shadow_of"):
                continue
            chat_id, message_id, route = row.get("chat_id"), row.get("message_id"), row.get("chosen_route")
            if not (isinstance(chat_id, str) and isinstance(message_id, str) and isinstance(route, str)):
                continue
            created = str(row.get("created_at") or "")
            key = (chat_id, message_id)
            current = index._by_answer.get(key)
            if current is None or created < current[0]:
                index._by_answer[key] = (created, route)
        return index

    def route(self, chat_id: Optional[str], answer_id: Optional[str]) -> Optional[str]:
        if not chat_id or not answer_id:
            return None
        found = self._by_answer.get((chat_id, answer_id))
        return found[1] if found else None

    def __len__(self) -> int:
        return len(self._by_answer)


# -- one chat -> its points ---------------------------------------------------------


def user_count(messages: Sequence[Dict[str, Any]]) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")


def trim_exchanges(messages: List[Dict[str, Any]], keep: int) -> Tuple[List[Dict[str, Any]], int]:
    """The messages from the start of the last ``keep`` exchanges on, and how
    many exchanges were cut. ``keep`` 0 keeps everything."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if keep <= 0 or len(users) <= keep:
        return messages, 0
    start = users[len(users) - keep]
    return messages[start:], len(users) - keep


async def chat_points(chat: Dict[str, Any], *, baseline: RuleBaseline, decisions: DecisionIndex,
                      keep_exchanges: int = 0) -> List[Dict[str, Any]]:
    """Every decision point of one exported chat, in branch order."""
    branch = [m for m in chat.get("messages") or [] if isinstance(m, dict)]
    options = convert.ReplayOptions.for_chat(chat)
    chat_id = chat.get("chat_id")
    user_positions = [i for i, m in enumerate(branch) if m.get("role") == "user"]
    points: List[Dict[str, Any]] = []
    previous_ts: Optional[float] = None
    for ordinal, pos in enumerate(user_positions, start=1):
        stored_user = branch[pos]
        replayed = convert.replay(branch[:pos + 1], options)
        messages = replayed.messages
        ts = convert.normalize_timestamp(stored_user.get("timestamp"))
        gap = ts - previous_ts if ts is not None and previous_ts is not None and ts >= previous_ts else None
        previous_ts = ts if ts is not None else previous_ts

        end = user_positions[ordinal] if ordinal < len(user_positions) else len(branch)
        stored_answer = branch[pos + 1:end]
        answer_msg = stored_answer[0] if stored_answer and stored_answer[0].get("role") == "assistant" else None
        answer_id = answer_msg.get("id") if answer_msg else None
        answer_tools = called_tool_names(convert.replay_answer(stored_answer))

        # What the privacy evaluator reads (user_text: Open WebUI's RAG block removed).
        text = user_text(messages[-1].get("content")) if messages and messages[-1].get("role") == "user" else ""
        privacy = baseline.privacy_matches(text)
        code_tool = baseline.code_tool(answer_tools)
        decision_route = decisions.route(chat_id, answer_id)
        label, source = weak_label(privacy, code_tool, decision_route)
        rules = await baseline.decide(messages)

        kept, trimmed = trim_exchanges(messages, keep_exchanges)
        points.append({
            "point_id": f"{chat_id}:{stored_user.get('id')}",
            "chat_id": chat_id,
            "user_id": chat.get("user_id"),
            "user_message_id": stored_user.get("id"),
            "answer_message_id": answer_id,
            "k": user_count(kept),
            "user_index": ordinal,
            "timestamp": ts,
            "gap_s": round(gap, 3) if gap is not None else None,
            "messages": kept,
            "trimmed_exchanges": trimmed,
            "answer_tools": answer_tools,
            "weak_label": label,
            "weak_source": source,
            "decision_route": decision_route,
            "rules_ruleset": baseline.ref,
            "rules_route": rules.route,
            "rules_decided_by": rules.decided_by,
            "privacy_kw_route": PRIVATE_ROUTE if privacy else None,
            "compacted": replayed.compacted,
            "answer_error": bool(answer_msg and answer_msg.get("error")),
            "answer_sources": int(answer_msg.get("sources") or 0) if answer_msg else 0,
        })
    return points


# -- the stratified sample ----------------------------------------------------------


def stratum(point: Dict[str, Any]) -> Tuple[str, str]:
    return (point.get("weak_label") or "none", point.get("rules_route") or "none")


def stratified_sample(points: Sequence[Dict[str, Any]], size: int, *, seed: int = DEFAULT_SAMPLE_SEED,
                      per_chat: int = DEFAULT_SAMPLE_PER_CHAT) -> List[str]:
    """``size`` point ids, round-robin across the (weak label, rules route)
    strata in a seeded order, at most ``per_chat`` per chat (0 = no cap);
    returned shuffled with the same seed."""
    rng = random.Random(seed)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for p in points:
        groups[stratum(p)].append(p)
    queues = []
    for key in sorted(groups):
        members = list(groups[key])
        rng.shuffle(members)
        queues.append(members)
    chosen: List[str] = []
    per: Dict[Any, int] = defaultdict(int)
    while len(chosen) < size and any(queues):
        for queue in queues:
            while queue:
                p = queue.pop()
                if per_chat and per[p.get("chat_id")] >= per_chat:
                    continue
                per[p.get("chat_id")] += 1
                chosen.append(p["point_id"])
                break
            if len(chosen) >= size:
                break
    rng.shuffle(chosen)
    return chosen


# -- the command ---------------------------------------------------------------------


def iter_chats(path: str) -> Iterator[Dict[str, Any]]:
    """chats.jsonl, one chat at a time (a line can be large)."""
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                chat = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from None
            if isinstance(chat, dict):
                yield chat


async def build(args: argparse.Namespace) -> int:
    baseline = RuleBaseline.from_file(args.ruleset, privacy_evaluator=args.privacy_evaluator,
                                      code_tools_evaluator=args.code_tools_evaluator)
    decisions = DecisionIndex()
    if args.decisions:
        rows, bad = read_jsonl(args.decisions, skip_bad=True)
        decisions = DecisionIndex.from_records(rows)
        warn(f"points: {len(decisions)} answers with a primary decision ({len(rows)} rows, {bad} unreadable)")
    light: List[Dict[str, Any]] = []
    chats = total = 0
    sources: Dict[str, int] = defaultdict(int)
    with open(args.out, "w", encoding="utf-8", newline="\n") as out:
        for chat in iter_chats(args.chats):
            chats += 1
            for point in await chat_points(chat, baseline=baseline, decisions=decisions,
                                           keep_exchanges=args.keep_exchanges):
                out.write(dumps(point) + "\n")
                total += 1
                sources[point["weak_source"] or "none"] += 1
                light.append({"point_id": point["point_id"], "chat_id": point["chat_id"],
                              "weak_label": point["weak_label"], "rules_route": point["rules_route"]})
            if chats % 200 == 0:
                warn(f"points: {chats} chats, {total} points so far")
    warn(f"points: {total} points from {chats} chats -> {args.out}; weak labels by source "
         f"{dict(sorted(sources.items()))}; rules = {baseline.ref}")
    if args.sample:
        ids = stratified_sample(light, args.sample, seed=args.seed, per_chat=args.sample_per_chat)
        wanted = {pid: i for i, pid in enumerate(ids)}
        picked: List[Optional[str]] = [None] * len(ids)
        with open(args.out, "r", encoding="utf-8") as fh:
            for line in fh:
                pid = json.loads(line).get("point_id")
                if pid in wanted:
                    picked[wanted[pid]] = line
        with open(args.sample_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(line for line in picked if line is not None)
        strata: Dict[Tuple[str, str], int] = defaultdict(int)
        for p in light:
            if p["point_id"] in wanted:
                strata[stratum(p)] += 1
        warn(f"points: sample of {len(ids)} -> {args.sample_out}; by (weak label, rules route) "
             + ", ".join(f"{a}/{b}: {n}" for (a, b), n in sorted(strata.items())))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.points", description=__doc__.split("\n\n")[0])
    parser.add_argument("--chats", required=True, help="chats.jsonl from export_chats.py")
    parser.add_argument("--out", required=True, help="points.jsonl to write")
    parser.add_argument("--decisions", help="decisions.jsonl exported from route-policy-db (BENCH.md)")
    parser.add_argument("--ruleset", default=DEFAULT_RULESET,
                        help=f"the rule-based baseline ruleset (default {DEFAULT_RULESET})")
    parser.add_argument("--privacy-evaluator", default="privacy", help="its privacy regex_text evaluator id")
    parser.add_argument("--code-tools-evaluator", default="code-tools", help="its tool_calls evaluator id")
    parser.add_argument("--keep-exchanges", type=int, default=0,
                        help="keep only each point's last N exchanges (0 = all)")
    parser.add_argument("--sample", type=int, default=0, help="also write a stratified sample of N points")
    parser.add_argument("--sample-out", help="where the sample goes (required with --sample)")
    parser.add_argument("--sample-per-chat", type=int, default=DEFAULT_SAMPLE_PER_CHAT,
                        help=f"at most N sampled points per chat (default {DEFAULT_SAMPLE_PER_CHAT}; 0 = no cap)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SAMPLE_SEED, help="sample seed")
    args = parser.parse_args(argv)
    if args.sample and not args.sample_out:
        parser.error("--sample needs --sample-out")
    if args.sample < 0 or args.keep_exchanges < 0 or args.sample_per_chat < 0:
        parser.error("--sample, --keep-exchanges and --sample-per-chat must be >= 0")
    for path in (args.chats, args.decisions):
        if path and not os.path.isfile(path):
            parser.error(f"{path}: no such file")
    return asyncio.run(build(args))


if __name__ == "__main__":
    sys.exit(main())
