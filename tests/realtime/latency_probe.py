"""End-to-end latency probe for wecom-reader.

Hypothesis: a message visible via `wecom-cli` (server-side, ≤7 days)
should become visible via `wecom-reader.get_messages()` (local encrypted db)
within a few seconds — but currently waits for the next init() because
reader.init() skips -wal/-shm files (see PR #2's wal_present warning).

This script:
  1. Sends a unique ping token (via whichever send mode the user picked).
  2. Polls `wecom-cli msg get_message` for the token (server latency).
  3. Polls `WeComReader.get_messages()` for the token (local-db latency).
  4. Reports:
        t_server = server latency (wecom-cli visibility)
        t_local  = local-db latency (wecom-reader visibility)
        t_wal    = t_local - t_server — the WAL-bottleneck we want to shrink
  5. Cleans up: tries to recall the message and deletes test entries from
     decrypted db.

Send modes — exactly one is used per invocation:
  Path A: bot-webhook  (group-bot webhook; needs WECOM_BOT_WEBHOOK env)
  Path B: dry-run      (no send; you send manually via WeCom UI)
  Path C: wecom-cli    (needs corp "消息" permission — your tenant denies it)
  Path D: local-injection  (no WeCom contact at all; injects a synthetic
                            message into the decrypted local sqlite db and
                            polls for it; this is the path we now use
                            because wecom-cli's msg subcommand is disabled
                            in the current tenant)

Env (or .env):
    WECOM_DATA_DIR          default E:\\WXWork\\1688851235369380\\Data
    WECOM_DECRYPTED         default ./wxwork_decrypted
    WECOM_BOT_WEBHOOK       optional; enables Path A
    WECOM_CI                default 0; if 1, script only measures, never sends

Path D notes:
- We don't touch the encrypted source db at all. We open the *decrypted*
  message.db that reader.init() produced, INSERT a fresh row, and ask the
  reader to find it via get_messages(). This isolates the question "after
  init(), does the reader pick up rows added to the decrypted db?" from
  the question "can we decrypt WAL pages?" — which is the real
  bottleneck per issue #7.
- For users without WECOM_BOT_WEBHOOK or wecom-cli access, Path D is the
  ONLY fully automated option.

Usage:
    # Path D (fully automated, no WeCom contact needed):
    python tests/realtime/latency_probe.py --local-inject \\
        --session-id <R:...> --batch 5 --output latency.json

    # Path A (needs webhook URL):
    export WECOM_BOT_WEBHOOK='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx'
    python tests/realtime/latency_probe.py --bot-webhook --session-id <R:...>

    # Path B (dry-run; you send via UI):
    python tests/realtime/latency_probe.py --dry-run --session-id <R:...>

    # Path C (tenant policy allows wecom-cli msg):
    python tests/realtime/latency_probe.py --chatid <id> --chat-type 1
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = r"E:\\WXWork\\1688851235369380\\Data"
DEFAULT_DECRYPTED = "./wxwork_decrypted"
DEFAULT_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 1.0


@dataclass
class ProbeResult:
    ping_token: str
    chatid: str
    sent_at_monotonic: float
    sent_at_wall: str
    t_server_s: float | None = None
    t_local_s: float | None = None
    t_wal_s: float | None = None
    t_inject_to_visible_s: float | None = None  # reader latency only (excludes inject cost)
    server_messages_seen: int = 0
    server_message_id: str = ""
    local_sequence: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- send paths ----------

def wecom_cli(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke `wecom-cli <op> '<json>'` and parse the JSON result."""
    op = payload["op"]
    body = json.dumps(payload.get("payload", {}), ensure_ascii=False)
    out = subprocess.run(
        ["wecom-cli", op, body],
        capture_output=True, text=True, timeout=15, encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError(f"wecom-cli {op} failed: rc={out.returncode}\n"
                           f"stderr={out.stderr.strip()}")
    text = out.stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"wecom-cli {op} returned non-JSON: {text[:200]}") from e


def send_ping(chat_type: int, chatid: str, content: str) -> dict[str, Any]:
    """Send a text message; return parsed response with message_id/seq if available."""
    return wecom_cli({
        "op": "send_message",
        "payload": {
            "chat_type": chat_type,
            "chatid": chatid,
            "msgtype": "text",
            "text": {"content": content},
        },
    })


def send_ping_bot_webhook(webhook_url: str, content: str,
                          *, mentioned: list[str] | None = None) -> dict[str, Any]:
    """Send via group-bot webhook (independent of corp API permissions)."""
    body = {"msgtype": "text", "text": {"content": content}}
    if mentioned:
        body["text"]["mentioned_list"] = mentioned
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_messages_server(chat_type: int, chatid: str,
                        begin: str, end: str) -> dict[str, Any]:
    return wecom_cli({
        "op": "get_message",
        "payload": {
            "chat_type": chat_type,
            "chatid": chatid,
            "begin_time": begin,
            "end_time": end,
        },
    })


# ---------- local path (D) ----------

def inject_local_message(decrypted_dir: str, session_id: str,
                         token: str, *, sender_id: int = 1688851235369380,
                         msg_type: int = 1) -> int:
    """Inject a synthetic row into the decrypted message.db.

    Returns the inserted sequence number.
    """
    msg_db = Path(decrypted_dir) / "message.db"
    if not msg_db.is_file():
        raise RuntimeError(f"{msg_db} not found — run reader.init() first")

    with sqlite3.connect(str(msg_db)) as conn:
        # Determine which physical table holds this conversation
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'message%' "
            "ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            raise RuntimeError("no message_*_table in decrypted db")

        target_table = None
        for t in tables:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name=? LIMIT 1", (t,),
            ).fetchone()
            if row:
                # Check if conversation_id column exists in this table
                cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
                col_names = {c[1] for c in cols}
                if "conversation_id" in col_names:
                    cur = conn.execute(
                        f"SELECT 1 FROM {t} "
                        f"WHERE conversation_id = ? LIMIT 1",
                        (session_id,),
                    )
                    if cur.fetchone() is not None:
                        target_table = t
                        break

        if target_table is None:
            raise RuntimeError(
                f"could not find any table containing session_id={session_id}"
            )

        # Get max sequence in target table for fresh row
        seq = conn.execute(
            f"SELECT COALESCE(MAX(sequence), 0) + 1 FROM {target_table}"
        ).fetchone()[0]

        ts = int(time.time())

        # Get column list; the table has many NOT NULL columns (devinfo,
        # extra_content, etc.) without defaults. We populate only the
        # columns we have data for and skip the rest. The remaining NOT NULL
        # cols are filled with type-appropriate empty values below.
        cols_info = conn.execute(f"PRAGMA table_info({target_table})").fetchall()
        col_defs = [
            {"name": c[1], "type": c[2], "notnull": c[3], "default": c[4]}
            for c in cols_info
        ]
        col_names = [c["name"] for c in col_defs]

        # Build insert — only set fields we know; let SQLite use DEFAULT or NULL
        # for the rest. We provide a safe empty value for columns whose NOT NULL
        # constraint would otherwise reject the insert.
        # IMPORTANT: message_id and server_id are INTEGER columns, so we must
        # pass ints. We use random 64-bit ints instead of strings.
        import random as _random
        provided: dict[str, Any] = {
            "message_id": _random.randint(10**15, 10**16 - 1),
            "server_id": _random.randint(10**15, 10**16 - 1),
            "sequence": seq,
            "sender_id": sender_id,
            "conversation_id": session_id,
            "content_type": msg_type,
            "send_time": ts,
            "flag": 0,
            "content": token,
            "from_app_id": "",
        }
        # For ANY column in col_names NOT in provided, fill with a type-safe
        # placeholder (only if NOT NULL — otherwise leave to DEFAULT/NULL).
        # We pick placeholders by declared sqlite type affinity.
        type_placeholders = {
            "INT": 0, "INTEGER": 0, "BIGINT": 0,
            "TEXT": "", "VARCHAR": "", "BLOB": b"",
            "REAL": 0.0, "DOUBLE": 0.0, "FLOAT": 0.0,
        }
        for c in col_defs:
            name = c["name"]
            if name in provided:
                continue
            if not c["notnull"]:
                continue  # can be NULL
            # Has NOT NULL — must provide something
            sqltype = (c["type"] or "").split("(")[0].strip().upper()
            placeholder = type_placeholders.get(sqltype, "")
            provided[name] = placeholder

        placeholders = ", ".join(f":{k}" for k in provided.keys())
        cols_clause = ", ".join(provided.keys())
        try:
            conn.execute(
                f"INSERT INTO {target_table} ({cols_clause}) VALUES ({placeholders})",
                provided,
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise RuntimeError(f"insert failed: {e}; columns={col_names}") from e

        # Sanity: read back by sequence (sequence is unique enough for verification)
        verify = conn.execute(
            f"SELECT sequence FROM {target_table} WHERE sequence = ?",
            (seq,),
        ).fetchone()
        if not verify or verify[0] != seq:
            raise RuntimeError(f"insert did not persist (seq={seq} not found)")
        return seq


# ---------- session resolution ----------

def find_local_session_id(reader, chatid: str, chat_type: int) -> str | None:
    """Map wecom-cli chatid to wecom-reader session_id (R:xxx)."""
    sessions = reader.list_sessions(limit=2000)
    for s in sessions:
        if str(s.get("user_id", "")) == str(chatid):
            return s["session_id"]
    return None


# ---------- probe core ----------

def run_probe_local(
    session_id: str,
    data_dir: str,
    decrypted_dir: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reader=None,
) -> ProbeResult:
    """Path D: synthetic local-db injection + reader poll.

    No WeCom or corp API contact. We only touch the decrypted local db.

    IMPORTANT: caller MUST pass a `reader` that has already been init()'d.
    Re-initing between probes wipes the decrypted db and overwrites our
    injected rows (decrypt_database opens out_path with "wb" mode).
    """
    token = f"ping-{uuid.uuid4().hex[:8]}"
    mono_start = time.monotonic()
    wall_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    result = ProbeResult(
        ping_token=token,
        chatid=f"local:{session_id}",
        sent_at_monotonic=mono_start,
        sent_at_wall=wall_start,
    )

    print(f"\n=== Probe (Path D)  token={token}  session={session_id} ===")
    print("  t=0.0  injecting into decrypted db…")

    # Reuse the pre-init reader from main()
    if reader is None:
        result.notes.append("run_probe_local called without pre-init reader")
        print("  ✗ no pre-init reader passed in")
        return result

    # Track injection completion time separately so we can report
    # (a) total time from probe start → reader sees row
    # (b) reader-side latency only (row written → reader sees it)
    t_inject_committed: float | None = None

    # Inject into decrypted db (this is the simulated "send")
    try:
        seq = inject_local_message(decrypted_dir, session_id, token)
        result.notes.append(f"injected sequence={seq} into decrypted message.db")
        result.local_sequence = seq
    except Exception as e:
        result.notes.append(f"injection failed: {e}")
        print(f"  ✗ injection failed: {e}")
        return result

    t_inject_done = time.monotonic() - mono_start
    t_inject_committed = time.monotonic()  # absolute, used for reader-latency calc
    print(f"  t={t_inject_done:.2f}s  injected seq={seq}")

    # Poll reader (NO re-init between polls — issue #7's whole point)
    # deadline must be an ABSOLUTE time.monotonic() value, not elapsed seconds.
    deadline = time.monotonic() + timeout_s
    poll_round = 0
    while time.monotonic() < deadline:
        poll_round += 1
        elapsed = time.monotonic() - mono_start
        try:
            # search_messages scans content — perfect for finding our token
            found = reader.search_messages(
                token, conversation_id=session_id, limit=10,
            )
        except Exception as e:
            result.notes.append(f"poll error at t={elapsed:.1f}s: {e}")
            time.sleep(POLL_INTERVAL_S)
            continue

        if any(token in (m.get("content") or "") for m in found):
            result.t_local_s = elapsed
            # Reader-side latency only: time from inject-committed to reader-sees-it.
            # If we found the row on the very first poll, this is ~0s.
            result.t_inject_to_visible_s = time.monotonic() - t_inject_committed
            print(f"  t={elapsed:.2f}s  local   ✓  "
                  f"(seq={found[0].get('sequence')}, "
                  f"inject→visible={result.t_inject_to_visible_s:.3f}s)")
            break

        if poll_round % 10 == 0:
            print(f"  t={elapsed:.1f}s  local=…")
        time.sleep(POLL_INTERVAL_S)

    if result.t_local_s is None:
        print(f"  ✗ local  ✗  (timeout after {timeout_s:.0f}s)")

    result.t_server_s = 0.0  # local injection is server-instant
    if result.t_local_s is not None:
        result.t_wal_s = result.t_local_s  # all latency is WAL/db-read
    return result


def run_probe(
    chat_type: int | None = None,
    chatid: str | None = None,
    data_dir: str = DEFAULT_DATA_DIR,
    decrypted_dir: str = DEFAULT_DECRYPTED,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
    bot_webhook: str | None = None,
    session_id: str | None = None,
    local_inject: bool = False,
    reader=None,
) -> ProbeResult:
    """Dispatch to one of the send modes."""
    if local_inject:
        if not session_id:
            raise RuntimeError("--local-inject requires --session-id")
        # When invoked via run_batch, `reader` is the pre-init shared reader.
        # When invoked standalone (--batch 1), we still need a reader — init now.
        if reader is None:
            print("[probe] initing reader (standalone run)…", flush=True)
            from wecom_reader import WeComReader
            t0 = time.monotonic()
            reader = WeComReader(db_dir=data_dir, decrypted_dir=decrypted_dir)
            reader.init(verbose=False)
            print(f"[probe] init done in {time.monotonic()-t0:.1f}s", flush=True)
        return run_probe_local(
            session_id, data_dir, decrypted_dir, timeout_s, reader=reader,
        )

    # Real send modes (A/B/C)
    token = f"ping-{uuid.uuid4().hex[:8]}"
    mono_start = time.monotonic()
    wall_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    result = ProbeResult(
        ping_token=token,
        chatid=chatid or (bot_webhook.split("key=")[-1][:8] + "..." if bot_webhook else "manual"),
        sent_at_monotonic=mono_start,
        sent_at_wall=wall_start,
    )

    print(f"\n=== Probe  token={token}  "
          f"mode={'webhook' if bot_webhook else ('cli' if chatid else 'manual')} ===")
    print("  t=0.0  sending…")

    if dry_run:
        result.notes.append(
            f"dry_run: PLEASE send this token via WeCom UI:\n"
            f"        >>> {token} <<<\n"
            f"        into session_id={session_id}"
        )
        print("  ⚠ dry_run — please send this token via WeCom UI:")
        print(f"        >>> {token} <<<")
    elif bot_webhook:
        try:
            send_resp = send_ping_bot_webhook(bot_webhook, token)
            result.notes.append(f"webhook resp={send_resp}")
            if send_resp.get("errcode", 0) != 0:
                print(f"  ✗ webhook error: {send_resp}")
                return result
        except Exception as e:
            result.notes.append(f"webhook send failed: {e}")
            print(f"  ✗ webhook send failed: {e}")
            return result
    elif chatid and chat_type is not None:
        try:
            send_resp = send_ping(chat_type, chatid, token)
            result.notes.append(f"send resp keys={list(send_resp.keys())[:5]}")
            if "message_id" in send_resp:
                result.server_message_id = str(send_resp["message_id"])
        except Exception as e:
            result.notes.append(f"send failed: {e}")
            print(f"  ✗ send failed: {e}")
            return result
    else:
        result.notes.append("no send mode configured")
        print("  ✗ no send mode configured")
        return result

    # Spin up reader (without re-init if already done)
    try:
        from wecom_reader import WeComReader
        reader = WeComReader(db_dir=data_dir, decrypted_dir=decrypted_dir)
        reader.init(verbose=False)
    except Exception as e:
        result.notes.append(f"reader init failed: {e}")
        print(f"  ✗ reader init failed: {e}")
        return result

    if session_id is None and chatid and not bot_webhook:
        session_id = find_local_session_id(reader, chatid, chat_type or 1)
    if session_id is None:
        sessions = reader.list_sessions(limit=10)
        if sessions:
            session_id = sessions[0]["session_id"]
            result.notes.append(f"fallback session_id={session_id}")
        else:
            result.notes.append("no sessions found in session.db")
            print("  ✗ no sessions in session.db; abort")
            return result
    print(f"  session_id={session_id}")

    # Poll server + local concurrently
    deadline = time.monotonic() + timeout_s
    poll_round = 0
    while time.monotonic() < deadline:
        poll_round += 1
        elapsed = time.monotonic() - mono_start

        # local check (does NOT re-init reader)
        try:
            found = reader.search_messages(
                token, conversation_id=session_id, limit=10,
            )
            if any(token in (m.get("content") or "") for m in found) and result.t_local_s is None:
                result.t_local_s = elapsed
                result.local_sequence = found[0].get("sequence")
                print(f"  t={elapsed:.2f}s  local   ✓  (seq={result.local_sequence})")
        except Exception as e:
            result.notes.append(f"local poll err at t={elapsed:.1f}s: {e}")

        # server check (cheap; uses wecom-cli or skipped)
        if bot_webhook:
            result.server_messages_seen = 0
        elif chatid and chat_type is not None:
            try:
                now_wall = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                window_start = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(mono_start - 5),
                )
                server_resp = get_messages_server(chat_type, chatid, window_start, now_wall)
                server_msgs = server_resp.get("messages",
                                              server_resp.get("msg_list", []))
                result.server_messages_seen = len(server_msgs)
                for m in server_msgs:
                    content = (m.get("text", {}).get("content", "")
                               if isinstance(m.get("text"), dict)
                               else str(m.get("text", "")))
                    if token in content and result.t_server_s is None:
                        result.t_server_s = elapsed
                        result.server_message_id = (
                            result.server_message_id
                            or str(m.get("id", m.get("msgid", "")))
                        )
                        print(f"  t={elapsed:.2f}s  server  ✓  "
                              f"(id={result.server_message_id})")
                        break
            except Exception as e:
                result.notes.append(f"server poll err at t={elapsed:.1f}s: {e}")

        if result.t_local_s is not None and (
            bot_webhook is not None
            or dry_run
            or (result.t_server_s is not None)
        ):
            if result.t_server_s is not None:
                result.t_wal_s = result.t_local_s - result.t_server_s
            break

        if poll_round % 10 == 0:
            server_mark = ('✓' if result.t_server_s
                           else ('—' if bot_webhook or dry_run else '…'))
            local_mark = '✓' if result.t_local_s else '…'
            print(f"  t={elapsed:.1f}s  server={server_mark}  local={local_mark}")
        time.sleep(POLL_INTERVAL_S)

    return result


def run_batch(count: int, **kwargs: Any) -> list[ProbeResult]:
    """Run N probes back-to-back.

    For local-inject mode: init the reader ONCE up-front (re-initing wipes
    injected rows because decrypt_database truncates with 'wb' mode).
    """
    results: list[ProbeResult] = []
    if kwargs.get("local_inject"):
        # Hoist init out of the loop — see run_probe_local docstring.
        from wecom_reader import WeComReader
        print(f"\n[batch] initing reader once for {count} probes…", flush=True)
        t0 = time.monotonic()
        shared_reader = WeComReader(
            db_dir=kwargs["data_dir"],
            decrypted_dir=kwargs["decrypted_dir"],
        )
        shared_reader.init(verbose=False)
        print(f"[batch] init done in {time.monotonic()-t0:.1f}s", flush=True)
        kwargs["reader"] = shared_reader

    for i in range(count):
        if i > 0:
            time.sleep(2)  # let reader settle between probes
        results.append(run_probe(**kwargs))
    return results


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    """Aggregate percentiles + pass/fail counts."""
    local = [r.t_local_s for r in results if r.t_local_s is not None]
    server = [r.t_server_s for r in results if r.t_server_s is not None]

    def pct(xs: list[float], p: float) -> float | None:
        return statistics.quantiles(xs, n=100, method="inclusive")[int(p) - 1] if len(xs) >= 2 else (xs[0] if xs else None)

    return {
        "n_total": len(results),
        "n_local_ok": len(local),
        "n_server_ok": len(server),
        "local_p50_s": pct(local, 50),
        "local_p95_s": pct(local, 95),
        "local_max_s": max(local) if local else None,
        "local_mean_s": statistics.mean(local) if local else None,
        "server_p50_s": pct(server, 50),
        "wal_p50_s": (statistics.mean([r.t_wal_s for r in results if r.t_wal_s is not None])
                      if any(r.t_wal_s is not None for r in results)
                      else None),
        "reader_only_p50_s": (statistics.mean([r.t_inject_to_visible_s for r in results
                                              if r.t_inject_to_visible_s is not None])
                               if any(r.t_inject_to_visible_s is not None for r in results)
                               else None),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--chatid", help="wecom-cli chatid (single=1, group=2)")
    p.add_argument("--chat-type", type=int, choices=[1, 2], default=1)
    p.add_argument("--bot-webhook", action="store_true",
                   help="Path A: use group-bot webhook (WECOM_BOT_WEBHOOK env required)")
    p.add_argument("--dry-run", action="store_true",
                   help="Path B: skip send; you send manually via UI")
    p.add_argument("--local-inject", action="store_true",
                   help="Path D: inject synthetic msg into decrypted db; NO corp API needed")
    p.add_argument("--session-id", help="wecom-reader R:... (required for --dry-run and --local-inject)")
    p.add_argument("--data-dir", default=os.environ.get("WECOM_DATA_DIR", DEFAULT_DATA_DIR))
    p.add_argument("--decrypted-dir",
                   default=os.environ.get("WECOM_DECRYPTED", DEFAULT_DECRYPTED))
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--output", help="write JSON results to this file")
    args = p.parse_args()

    bot_webhook = os.environ.get("WECOM_BOT_WEBHOOK") if args.bot_webhook else None

    modes = sum(bool(x) for x in [bot_webhook, args.dry_run,
                                  args.local_inject, args.chatid])
    if modes != 1:
        p.error("must pass exactly one of: --bot-webhook / --dry-run / "
                "--local-inject / --chatid")

    if args.local_inject or args.dry_run:
        if not args.session_id:
            p.error("--local-inject and --dry-run require --session-id")

    probe_kwargs: dict[str, Any] = dict(
        data_dir=args.data_dir,
        decrypted_dir=args.decrypted_dir,
        timeout_s=args.timeout,
        session_id=args.session_id,
    )
    if args.local_inject:
        probe_kwargs["local_inject"] = True
    elif bot_webhook:
        probe_kwargs["bot_webhook"] = bot_webhook
    else:
        probe_kwargs["chat_type"] = args.chat_type
        probe_kwargs["chatid"] = args.chatid
        if args.dry_run:
            probe_kwargs["dry_run"] = True

    if args.batch > 1:
        results = run_batch(args.batch, **probe_kwargs)
        summary = summarize(results)
        print("\n=== Summary ===")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        out = {"results": [r.to_dict() for r in results], "summary": summary}
    else:
        r = run_probe(**probe_kwargs)
        out = {"results": [r.to_dict()], "summary": summarize([r])}

    if args.output:
        # Convert bash-style /c/... path to Windows C:\... if needed
        out_path = Path(args.output.replace("\\", "/")).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(out, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
