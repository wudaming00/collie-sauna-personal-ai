"""Memory subsystem — the differentiator vs Claude Code's linear-scan index.

Three tiers (Letta x Hermes), one SQLite file, scoped per project:

  CORE      pinned blocks, char-capped, loaded every turn (in the VOLATILE tail
            of the prompt so they can update mid-session without busting cache).
  ARCHIVAL  facts store: text + keys + embedding, retrieved ON DEMAND via a
            HYBRID query (BM25 over FTS5  +  dense cosine  ->  RRF fusion).
  (RECALL   verbatim message log lives in the recorder's events; a future
            messages_fts seam can be added the same way as facts_fts.)

The retrieval that fixes pain #1 is `recall()`: sparse + dense + RRF. With
HashEmbedding the dense arm is weak but the pipeline is real; drop in bge-m3 and
precision jumps with zero changes above this file.

Char-cap consolidation (pain #3): `set_block` refuses to overflow a CORE block —
the caller (or a consolidation model) must merge/evict. That is the LLM-in-loop
GC that stops the 118-file balloon.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import sqlite3
import time

from .embeddings import EmbeddingProvider, make_embedding, cosine


# ``proposed`` claims are deliberately absent from normal recall.  A host must
# promote them after user attestation or independent verification; keeping the
# accepted set here (rather than sprinkling status checks through the query
# paths) makes that trust boundary auditable.
RECALLABLE_STATUSES = frozenset(("active", "attested", "verified"))
MEMORY_STATUSES = RECALLABLE_STATUSES | frozenset(("proposed", "rejected", "invalidated"))

# A fact answers "what is true"; the remaining kinds answer "how this owner / Collie tends to
# work".  Keeping the distinction in the durable row matters because routing may consume a
# confirmed preference or repeated observed habit, but must never turn an arbitrary model-authored
# observation into policy.  Existing callers remain facts by default.
MEMORY_KINDS = frozenset((
    "fact", "preference", "habit", "procedure", "decision", "identity", "observation",
))
MEMORY_SUBJECTS = frozenset(("owner", "collie", "device", "project", "mission", "external"))
PROFILE_KINDS = frozenset(("preference", "habit"))


# Memory is a retrieval surface, not a credential store.  In particular, a
# model-authored proposal can be copied into SQLite's WAL before a reviewer ever
# sees it.  Redacting after INSERT therefore does not help: admission has to
# happen before distillation, embedding, or the first database statement.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(?:password|passwd|pwd|passcode|login[\s_-]*secret|client[\s_-]*secret|
        api[\s_-]*key|access[\s_-]*token|refresh[\s_-]*token|auth[\s_-]*token|token|
        bearer[\s_-]*token|private[\s_-]*key|totp[\s_-]*secret|
        recovery[\s_-]*(?:codes?|keys?)|backup[\s_-]*(?:codes?|keys?)|
        verification[\s_-]*codes?|mfa[\s_-]*code|2fa[\s_-]*code|otp|secret|credential)
    \b(?:[\s_-]*(?:value|bytes|plaintext))?[\"']?\s*
    (?:\s+(?:for|of|on)\s+[^\r\n:=]{1,80}?)?
    (?:is\s+|was\s+|=|:|->|\bto\s+)\s*[\"']?([^\s,;\"'}\]]{1,})
    """)
_VALUE_BEFORE_SECRET_RE = re.compile(
    r"""(?ix)
    \b(?:use|enter|store|save|set)\s+[\"']?([^\s,;\"'}\]]{1,})[\"']?
    \s+as\s+(?:the\s+|your\s+)?
    (?:password|passwd|pwd|passcode|client[\s_-]*secret|api[\s_-]*key|
       access[\s_-]*token|refresh[\s_-]*token|auth[\s_-]*token|token|
       private[\s_-]*key|totp[\s_-]*secret|recovery[\s_-]*(?:codes?|keys?)|
       backup[\s_-]*(?:codes?|keys?)|verification[\s_-]*codes?|
       mfa[\s_-]*code|2fa[\s_-]*code|otp|secret|credential)\b
    """)
_VALUE_IS_SECRET_LABEL_RE = re.compile(
    r"""(?ix)
    \b([^\s,;\"'}\]]{1,})\s+is\s+(?:the\s+|your\s+)
    (?:[A-Za-z0-9_.-]+\s+){0,4}
    (?:password|passwd|pwd|passcode|client[\s_-]*secret|api[\s_-]*key|
       access[\s_-]*token|refresh[\s_-]*token|auth[\s_-]*token|token|
       private[\s_-]*key|totp[\s_-]*secret|recovery[\s_-]*(?:codes?|keys?)|
       backup[\s_-]*(?:codes?|keys?)|verification[\s_-]*codes?|
       mfa[\s_-]*code|2fa[\s_-]*code|otp|secret|credential)\b
    """)
_OTP_CONTEXT_RE = re.compile(
    r"(?i)\b(?:otp|one[\s_-]*time(?:\s+password)?|verification[\s_-]*code|"
    r"mfa[\s_-]*code|2fa[\s_-]*code)\b[^\r\n\d]{0,24}(\d{4,8})\b")
_TOKEN_FORMAT_RE = re.compile(
    r"(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\bBasic\s+[A-Za-z0-9+/=]{8,}|"
    r"\b(?:sk|rk|pk)[-_](?:live|test|proj)?[-_A-Za-z0-9]{12,}|"
    r"\bgh[opusr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,}|\bcv1_[A-Za-z0-9_-]{24,96}|"
    r"\bAKIA[A-Z0-9]{16}|\bAIza[A-Za-z0-9_-]{30,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"\botpauth://|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
_SECRET_QUERY_RE = re.compile(
    r"(?i)(?:[?&]|\b)(?:access_token|refresh_token|api_key|client_secret|password)="
    r"([^&\s]{4,})")
_NON_SECRET_VALUES = frozenset((
    "available", "configured", "disabled", "empty", "enabled", "false", "hidden",
    "missing", "none", "not-configured", "not_configured", "null", "redacted",
    "required", "true", "unavailable", "unknown",
))


def _memory_admission_text(value, _depth=0, _seen=None) -> str:
    """Serialize untrusted proposal metadata without invoking custom objects."""
    if _depth > 12:
        return "<unsupported metadata>"
    if _seen is None:
        _seen = set()
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text if len(text) <= 1_000_000 else "<unsupported metadata>"
    if isinstance(value, bytes):
        if len(value) > 1_000_000:
            return "<unsupported metadata>"
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        if len(value) > 200 or id(value) in _seen:
            return "<unsupported metadata>"
        _seen.add(id(value))
        parts = []
        for key, item in value.items():
            if not isinstance(key, (str, int, float, bool)):
                return "<unsupported metadata>"
            parts.append("%s: %s" % (
                key, _memory_admission_text(item, _depth + 1, _seen)))
        _seen.remove(id(value))
        return "\n".join(parts)
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > 200 or id(value) in _seen:
            return "<unsupported metadata>"
        _seen.add(id(value))
        result = "\n".join(
            _memory_admission_text(item, _depth + 1, _seen) for item in value)
        _seen.remove(id(value))
        return result
    # Calling an arbitrary __str__ supplied through a model-facing adapter is
    # unnecessary and can have side effects.  Unknown metadata fails closed.
    return "<unsupported metadata>"


def contains_memory_secret(value) -> bool:
    """Conservatively identify plaintext credentials in model-originated memory.

    Ordinary facts may discuss passwords/tokens.  We reject concrete assignments,
    OTP-shaped values with authentication context, well-known token formats,
    credential query parameters, and unsupported structured objects.  The value
    itself is never returned or included in an exception.
    """
    text = _memory_admission_text(value)
    if text == "<unsupported metadata>" or "<unsupported metadata>" in text:
        return True
    if _TOKEN_FORMAT_RE.search(text) or _OTP_CONTEXT_RE.search(text) \
            or _SECRET_QUERY_RE.search(text):
        return True
    for pattern in (
            _SECRET_ASSIGNMENT_RE, _VALUE_BEFORE_SECRET_RE,
            _VALUE_IS_SECRET_LABEL_RE):
        for match in pattern.finditer(text):
            candidate = match.group(1).strip(".()[]{}<>\"'").lower()
            if candidate and candidate not in _NON_SECRET_VALUES:
                return True
    return False


def _now() -> int:
    # NOTE: time.time() is fine here; determinism handled by callers/tests.
    return int(time.time())


def project_scope(cwd: str = "") -> str:
    """The memory scope for a working directory: the CODEBASE, not the surface it was reached from.

    Every entry point used to name this after itself — the web app passed "web", every argparse
    default passed "demo" — so one machine, one checkout and one dog kept two memories that could
    not see each other, divided by which window the person happened to type into. Nothing about a
    project changes when you move from a chat panel to Slack, so nothing about its memory should:
    what was learned answering in one place is exactly what is missing in the other.

    A git checkout is scoped by its ROOT, so a subdirectory is the same project as the repo above
    it. Outside a checkout the directory itself is the project.

    The display label remains the basename, but the trust boundary includes a stable digest of the
    canonical root. Two unrelated customer repositories called ``backend`` must never recall each
    other's facts. Legacy basename-only rows remain available through an explicit ``--project
    backend`` so an upgrade neither deletes nor silently reassigns ambiguous historical memory.
    """
    start = os.path.abspath(cwd or os.getcwd())
    root = start
    while True:
        if os.path.exists(os.path.join(root, ".git")):
            break
        parent = os.path.dirname(root)
        if parent == root:                  # walked to the filesystem root: not a checkout
            root = start
            break
        root = parent
    # never "global": that is the read-by-everyone tier, and a scope that fell back into it would
    # quietly publish one project's facts to every other. normcase matters on Windows, where two
    # spellings of the same checkout are the same trust boundary.
    canonical = os.path.normcase(os.path.realpath(root))
    label = os.path.basename(canonical).lower() or "default"
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return "%s@%s" % (label, digest)


class BlockOverflow(Exception):
    """Raised when a CORE block write would exceed its char cap."""


class MemorySecretRejected(ValueError):
    """Credential material was refused before any durable Memory write."""


class SqliteMemory:
    def __init__(self, path: str, embedder: EmbeddingProvider | None = None,
                 reranker=None, distiller=None):
        self.path = path
        in_memory = (str(path) == ":memory:" or
                     (str(path).startswith("file:") and
                      "mode=memory" in str(path)))
        parent = "" if in_memory else os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
            state_root = os.path.abspath(os.environ.get("COLLIE_STATE_DIR") or
                                         os.path.expanduser("~/.collie"))
            try:
                private_parent = (os.path.commonpath((os.path.realpath(parent),
                                                      os.path.realpath(state_root)))
                                  == os.path.realpath(state_root))
            except ValueError:
                private_parent = False
            if private_parent:
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
        # embedder=None -> BM25-only (dense arm disabled). This is the low-spec / offline default:
        # a REAL embedder (granite) is added when available, but we NEVER fall back to HashEmbedding —
        # measured on LOCOMO, hash-dense (0.346) is WORSE than pure BM25 (0.526): its bag-of-words
        # cosines inject noise into RRF and actively hurt recall. So no embedder => sparse-only.
        self.embedder = embedder
        self.embed_model = self.embedder.name if self.embedder else "bm25-only"
        self.reranker = reranker          # optional cross-encoder over the fused top-k
        self.distiller = distiller        # optional (text,keys)->clean fact str, write-time
        # check_same_thread=False: the ACP path builds the harness on the asyncio event-loop
        # thread but runs it (recall/remember) inside a run_in_executor worker thread — the default
        # same-thread guard would raise ProgrammingError on the first memory access and break every
        # ACP prompt. Access stays sequential (one run at a time), and each web request uses its own
        # connection, so relaxing the check introduces no real concurrent-write hazard.
        # timeout=30 + WAL + busy_timeout so CONCURRENT real-provider runs (two web tabs at once)
        # don't lose their answer to `database is locked`: every non-mock run WRITES memory at
        # consolidation, and the default busy_timeout=0 makes the loser raise immediately, which the
        # loop reports as res.error and the UI then discards the (already-computed) answer. Mirrors
        # recorder.py, which was given this treatment but memory was not.
        if not in_memory and not os.path.exists(path):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        if not in_memory:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.has_fts = True
        from . import memory_sync as _memory_sync
        _memory_sync.prepare_connection(self)
        self._init_schema()
        _memory_sync.install(self)

    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        c = self.db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS blocks(
            id INTEGER PRIMARY KEY, scope TEXT, label TEXT,
            value TEXT, char_limit INTEGER, updated_at INTEGER,
            UNIQUE(scope, label))""")
        c.execute("""CREATE TABLE IF NOT EXISTS facts(
            id INTEGER PRIMARY KEY, project TEXT, text TEXT, keys TEXT,
            importance REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0,
            last_access INTEGER, created_at INTEGER, superseded_by INTEGER,
            embed_model TEXT, embedding TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL DEFAULT 'host', evidence TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
            review_source TEXT NOT NULL DEFAULT '',
            review_evidence TEXT NOT NULL DEFAULT '',
            review_provenance TEXT NOT NULL DEFAULT '', reviewed_at INTEGER,
            kind TEXT NOT NULL DEFAULT 'fact',
            subject TEXT NOT NULL DEFAULT 'project',
            confidence REAL NOT NULL DEFAULT 0.5,
            observations INTEGER NOT NULL DEFAULT 1,
            expires_at INTEGER,
            device_id TEXT NOT NULL DEFAULT '',
            mission_id TEXT NOT NULL DEFAULT '',
            attribute TEXT NOT NULL DEFAULT '',
            value_json TEXT NOT NULL DEFAULT '',
            valid_from INTEGER,
            valid_to INTEGER,
            observed_at INTEGER,
            conflict_key TEXT NOT NULL DEFAULT '',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            context_json TEXT NOT NULL DEFAULT '{}',
            counter_observations INTEGER NOT NULL DEFAULT 0,
            relations_json TEXT NOT NULL DEFAULT '[]')""")
        # In-place migration for every pre-claim memory.db.  Existing rows were
        # already eligible for recall, so changing them to ``proposed`` would be
        # a destructive trust downgrade.  They stay recallable and are marked
        # ``legacy`` so a future review UI can distinguish them from evidenced
        # claims.  The re-check handles two Collie processes racing this same
        # idempotent migration.
        fact_cols = {r[1] for r in c.execute("PRAGMA table_info(facts)")}
        for name, decl in (
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("source", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("evidence", "TEXT NOT NULL DEFAULT ''"),
                ("provenance", "TEXT NOT NULL DEFAULT ''"),
                ("scope", "TEXT NOT NULL DEFAULT ''"),
                ("review_source", "TEXT NOT NULL DEFAULT ''"),
                ("review_evidence", "TEXT NOT NULL DEFAULT ''"),
                ("review_provenance", "TEXT NOT NULL DEFAULT ''"),
                ("reviewed_at", "INTEGER"),
                ("kind", "TEXT NOT NULL DEFAULT 'fact'"),
                ("subject", "TEXT NOT NULL DEFAULT 'project'"),
                ("confidence", "REAL NOT NULL DEFAULT 0.5"),
                ("observations", "INTEGER NOT NULL DEFAULT 1"),
                ("expires_at", "INTEGER"),
                ("device_id", "TEXT NOT NULL DEFAULT ''"),
                ("mission_id", "TEXT NOT NULL DEFAULT ''"),
                ("attribute", "TEXT NOT NULL DEFAULT ''"),
                ("value_json", "TEXT NOT NULL DEFAULT ''"),
                ("valid_from", "INTEGER"),
                ("valid_to", "INTEGER"),
                ("observed_at", "INTEGER"),
                ("conflict_key", "TEXT NOT NULL DEFAULT ''"),
                ("evidence_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("context_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("counter_observations", "INTEGER NOT NULL DEFAULT 0"),
                ("relations_json", "TEXT NOT NULL DEFAULT '[]'")):
            if name in fact_cols:
                continue
            try:
                c.execute("ALTER TABLE facts ADD COLUMN %s %s" % (name, decl))
            except sqlite3.OperationalError:
                current = {r[1] for r in c.execute("PRAGMA table_info(facts)")}
                if name not in current:
                    raise
            fact_cols.add(name)
        # ``scope`` is a claim's trust boundary.  For old rows the only scope
        # that existed was ``project``, so preserve that exact meaning.
        c.execute("UPDATE facts SET scope=project WHERE scope IS NULL OR scope='' ")
        # Older builds consolidated by project alone.  That could leave an
        # allowed-scope predecessor hidden behind a successor the caller is not
        # authorized to retrieve.  Repair those historical links once (and
        # harmlessly on every open); valid same-project/same-scope chains stay
        # intact.
        c.execute("""UPDATE facts SET superseded_by=NULL
                     WHERE superseded_by IS NOT NULL AND NOT EXISTS(
                         SELECT 1 FROM facts AS successor
                         WHERE successor.id=facts.superseded_by
                           AND COALESCE(successor.project,'')=COALESCE(facts.project,'')
                           AND COALESCE(successor.scope,'')=COALESCE(facts.scope,''))""")
        c.execute("""CREATE INDEX IF NOT EXISTS facts_recall_scope
                     ON facts(project,status,superseded_by)""")
        # ``facts_recall_scope`` predates claim scopes and cannot be changed in
        # place on existing databases.  Keep it for compatibility and add a
        # scope-aware index under a new name for every scoped read path below.
        c.execute("""CREATE INDEX IF NOT EXISTS facts_scope_recall_v2
                     ON facts(project,scope,status,superseded_by)""")
        c.execute("""CREATE INDEX IF NOT EXISTS facts_profile_v3
                     ON facts(project,scope,kind,status,attribute,device_id,superseded_by)""")
        c.execute("""CREATE INDEX IF NOT EXISTS facts_temporal_v4
                     ON facts(project,scope,conflict_key,valid_from,valid_to,status)""")
        # Relationship data is a retractable index.  Every edge points to one claim and graph
        # reads join back through the claim's trust/scope/time admission before traversal.
        c.execute("""CREATE TABLE IF NOT EXISTS memory_entities(
            entity_id TEXT PRIMARY KEY, normalized TEXT NOT NULL, display_name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'entity', created_at INTEGER NOT NULL)""")
        c.execute("""CREATE INDEX IF NOT EXISTS memory_entities_name_v1
                     ON memory_entities(normalized)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memory_edges(
            edge_id TEXT PRIMARY KEY, claim_id INTEGER NOT NULL,
            subject_id TEXT NOT NULL, predicate TEXT NOT NULL, object_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(claim_id,subject_id,predicate,object_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS memory_edges_claim_v1 ON memory_edges(claim_id)")
        c.execute("CREATE INDEX IF NOT EXISTS memory_edges_subject_v1 ON memory_edges(subject_id)")
        c.execute("CREATE INDEX IF NOT EXISTS memory_edges_object_v1 ON memory_edges(object_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS memory_evidence(
            evidence_id TEXT PRIMARY KEY,source_type TEXT NOT NULL,source_ref TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,observed_at INTEGER NOT NULL,sensitivity TEXT NOT NULL,
            retention TEXT NOT NULL,excerpt TEXT NOT NULL DEFAULT '',origin_device TEXT NOT NULL,
            created_at INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memory_claim_evidence(
            claim_id TEXT NOT NULL,evidence_id TEXT NOT NULL,relation TEXT NOT NULL DEFAULT 'supports',
            created_at INTEGER NOT NULL,PRIMARY KEY(claim_id,evidence_id,relation))""")
        c.execute("""CREATE TABLE IF NOT EXISTS memory_graph_extractions(
            extraction_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL,extractor TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',input_hash TEXT NOT NULL,relations_json TEXT NOT NULL,
            status TEXT NOT NULL,created_at INTEGER NOT NULL)""")
        # Basename-only projects were the pre-hash boundary.  Candidate rows do
        # not rewrite or duplicate facts; they let a single known canonical
        # checkout read the legacy boundary, and make that alias fail closed as
        # soon as a second same-basename checkout is observed.  A local host can
        # then select one canonical owner explicitly.
        c.execute("""CREATE TABLE IF NOT EXISTS project_alias_candidates(
            legacy_project TEXT NOT NULL,
            canonical_project TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL,
            PRIMARY KEY(legacy_project, canonical_project))""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS project_alias_selected_v1
                     ON project_alias_candidates(legacy_project)
                     WHERE selected=1""")
        try:
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(text, keys, content='facts', content_rowid='id')""")
        except sqlite3.OperationalError:
            self.has_fts = False  # FTS5 not compiled in -> LIKE fallback
        self.db.commit()

    # ------------------------------------------------------------------ #
    #  CORE blocks
    # ------------------------------------------------------------------ #
    def set_block(self, scope: str, label: str, value: str, char_limit: int = 1500) -> None:
        if (contains_memory_secret({"scope": scope, "label": label, "value": value})
                or contains_memory_secret("%s: %s" % (
                    _memory_admission_text(label), _memory_admission_text(value)))):
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory")
        if len(value) > char_limit:
            raise BlockOverflow(
                "block %s/%s: %d > %d chars — consolidate before writing"
                % (scope, label, len(value), char_limit))
        self.db.execute(
            """INSERT INTO blocks(scope,label,value,char_limit,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(scope,label) DO UPDATE SET
                 value=excluded.value, char_limit=excluded.char_limit,
                 updated_at=excluded.updated_at""",
            (scope, label, value, char_limit, _now()))
        self.db.commit()

    def core_blocks(self, scopes: list[str]) -> list[sqlite3.Row]:
        expanded = []
        for raw_scope in scopes:
            scope = str(raw_scope or "")
            if scope and scope not in expanded:
                expanded.append(scope)
            if scope.startswith("project:"):
                legacy = self._legacy_alias(scope[len("project:"):], register=True)
                legacy_scope = "project:" + legacy if legacy else ""
                if legacy_scope and legacy_scope not in expanded:
                    expanded.append(legacy_scope)
        if not expanded:
            return []
        q = ",".join("?" * len(expanded))
        return self.db.execute(
            "SELECT * FROM blocks WHERE scope IN (%s) ORDER BY scope, label" % q,
            expanded).fetchall()

    # ------------------------------------------------------------------ #
    #  ARCHIVAL write
    # ------------------------------------------------------------------ #
    @staticmethod
    def _statuses(statuses=None) -> tuple[str, ...]:
        values = tuple(statuses or RECALLABLE_STATUSES)
        invalid = set(values) - MEMORY_STATUSES
        if invalid:
            raise ValueError("invalid memory status: %s" % ", ".join(sorted(invalid)))
        return values

    @staticmethod
    def _allowed_scopes(project: str, allowed_scopes=None) -> tuple[str, ...]:
        """Normalize the trust scopes a caller has explicitly been given.

        Historically ``project`` was the only boundary.  Migrated rows use it
        as their scope, while globally shared rows use ``global``; accepting
        those two scopes by default preserves that API without making any
        other scope in the same project implicitly readable.
        """
        if allowed_scopes is None:
            values = (str(project or "global"), "global")
        elif isinstance(allowed_scopes, str):
            values = (allowed_scopes,)
        else:
            values = tuple(allowed_scopes)
        # Stable de-duplication keeps SQL parameters deterministic.  Empty or
        # None scope names grant no authority rather than becoming wildcards.
        return tuple(dict.fromkeys(
            str(value) for value in values if value is not None and str(value)))

    @staticmethod
    def claim_boundary(project: str) -> dict[str, str]:
        """Return the physical project/scope used for a logical claim write."""
        value = str(project or "global")
        return {"project": value, "scope": value}

    @staticmethod
    def _legacy_project_name(project: str) -> str:
        """Return the old basename scope for one hashed canonical project."""
        project = str(project or "")
        match = re.fullmatch(r"(.+)@([0-9a-f]{24})", project)
        if not match or contains_memory_secret(project):
            return ""
        return match.group(1)

    def _legacy_alias(self, project: str, *, register: bool = True) -> str:
        """Resolve a read-only legacy alias, failing closed on ambiguity."""
        project = str(project or "global")
        legacy = self._legacy_project_name(project)
        if not legacy:
            return ""
        known = {project}
        # A hashed project may already have facts/blocks from a previous run
        # before this migration table existed. Discover every same-basename
        # canonical boundary before granting the first read-only alias.
        for row in self.db.execute("SELECT DISTINCT project FROM facts").fetchall():
            candidate = str(row["project"] or "")
            if self._legacy_project_name(candidate) == legacy:
                known.add(candidate)
        for row in self.db.execute("SELECT DISTINCT scope FROM blocks").fetchall():
            scope = str(row["scope"] or "")
            candidate = scope[8:] if scope.startswith("project:") else ""
            if self._legacy_project_name(candidate) == legacy:
                known.add(candidate)
        existing = {
            row["canonical_project"] for row in self.db.execute(
                """SELECT canonical_project FROM project_alias_candidates
                   WHERE legacy_project=?""", (legacy,)).fetchall()
        }
        missing = sorted(known - existing)
        if register and missing and not self.db.in_transaction:
            try:
                with self.db:
                    self.db.executemany(
                        """INSERT OR IGNORE INTO project_alias_candidates(
                               legacy_project,canonical_project,selected,first_seen)
                           VALUES(?,?,0,?)""",
                        [(legacy, candidate, _now()) for candidate in missing])
            except sqlite3.OperationalError:
                # A status/read path must not fail open when another process is
                # migrating.  Without a durable candidate we grant no alias.
                return ""
        rows = self.db.execute(
            """SELECT canonical_project,selected FROM project_alias_candidates
               WHERE legacy_project=? ORDER BY canonical_project""", (legacy,)).fetchall()
        selected = [row["canonical_project"] for row in rows if row["selected"]]
        if selected:
            return legacy if selected == [project] else ""
        return legacy if len(rows) == 1 and rows[0]["canonical_project"] == project else ""

    def legacy_project_alias_status(self, project: str) -> dict:
        """Expose continuity state without exposing paths or changing legacy facts."""
        project = str(project or "global")
        legacy = self._legacy_project_name(project)
        if not legacy:
            return {"status": "not_applicable", "legacy_project": "",
                    "canonical_project": project, "candidates": [], "selected": ""}
        self._legacy_alias(project, register=True)
        rows = self.db.execute(
            """SELECT canonical_project,selected FROM project_alias_candidates
               WHERE legacy_project=? ORDER BY canonical_project""", (legacy,)).fetchall()
        candidates = [row["canonical_project"] for row in rows]
        selected = next((row["canonical_project"] for row in rows if row["selected"]), "")
        if selected:
            status = "selected" if selected == project else "selected_elsewhere"
        elif len(candidates) == 1:
            status = "read_only_available"
        else:
            status = "ambiguous_selection_required"
        return {"status": status, "legacy_project": legacy,
                "canonical_project": project, "candidates": candidates,
                "selected": selected}

    def select_legacy_project_alias(self, project: str) -> dict:
        """Explicitly assign an ambiguous basename-only history to one checkout.

        Facts remain in the legacy boundary and are only aliased for reads.  This
        makes selection reversible and avoids copying rows into multiple repos.
        """
        project = str(project or "")
        legacy = self._legacy_project_name(project)
        if not legacy:
            raise ValueError("a hashed canonical project is required")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT OR IGNORE INTO project_alias_candidates(
                       legacy_project,canonical_project,selected,first_seen)
                   VALUES(?,?,0,?)""", (legacy, project, _now()))
            self.db.execute(
                "UPDATE project_alias_candidates SET selected=0 WHERE legacy_project=?",
                (legacy,))
            self.db.execute(
                """UPDATE project_alias_candidates SET selected=1
                   WHERE legacy_project=? AND canonical_project=?""", (legacy, project))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.legacy_project_alias_status(project)

    def _read_boundary(self, project: str, allowed_scopes=None, *, include_global=True):
        project = str(project or "global")
        scopes = self._allowed_scopes(project, allowed_scopes)
        projects = [project]
        if include_global and "global" not in projects:
            projects.append("global")
        legacy = self._legacy_alias(project, register=True)
        if legacy:
            projects.append(legacy)
            # The alias inherits only authority over the canonical root scope;
            # an unrelated explicit scope cannot opt into legacy data.
            if project in scopes and legacy not in scopes:
                scopes = (*scopes, legacy)
        return tuple(dict.fromkeys(projects)), tuple(dict.fromkeys(scopes)), legacy

    def _nearest(self, vec, project: str, statuses=None, *, embed_model: str | None = None,
                 exclude_id: int | None = None, scope: str | None = None):
        """Nearest accepted fact inside one project *and* trust scope."""
        best_id, best = None, 0.0
        statuses = self._statuses(statuses)
        q = ",".join("?" * len(statuses))
        scope = str(scope or project)
        now = _now()
        where = ["project=?", "scope=?", "superseded_by IS NULL",
                 "(expires_at IS NULL OR expires_at>?)",
                 "created_at<=?", "(valid_from IS NULL OR valid_from<=?)",
                 "(valid_to IS NULL OR valid_to>?)",
                 "status IN (%s)" % q]
        params = [project, scope, now, now, now, now, *statuses]
        # Embeddings from different models are not in the same vector space.
        # Promotion can happen in a later process whose current embedder differs,
        # so match using the proposal's stored model, not self.embed_model.
        if embed_model is not None:
            where.append("embed_model=?")
            params.append(embed_model)
        if exclude_id is not None:
            where.append("id<>?")
            params.append(int(exclude_id))
        for r in self.db.execute(
                "SELECT id, embedding FROM facts WHERE " + " AND ".join(where),
                params).fetchall():
            try:
                s = cosine(vec, json.loads(r["embedding"]))
            except Exception:
                continue
            if s > best:
                best, best_id = s, r["id"]
        return best_id, best

    def remember(self, text: str, keys: str = "", project: str = "global",
                 importance: float = 0.5, consolidate: bool = True,
                 dedup_at: float = 0.93, created_at: int | None = None,
                 status: str = "active", source: str = "host", evidence: str = "",
                 provenance: str = "", scope: str | None = None, *, kind: str = "fact",
                 subject: str = "project", confidence: float = 0.5,
                 observations: int = 1, expires_at: int | None = None,
                 device_id: str = "", mission_id: str = "", attribute: str = "",
                 value=None, valid_from: int | None = None, valid_to: int | None = None,
                 observed_at: int | None = None, conflict_key: str = "",
                 context: dict | None = None, counter_observations: int = 0,
                 _commit: bool = True, _distill: bool = True) -> int:
        """Store a memory claim.

        Direct host callers retain the historic ``active`` default.  Model-facing
        tools must explicitly pass ``status='proposed'``; this split keeps old
        importers and verified consolidation compatible without allowing an
        agent assertion to silently become a durable fact.
        """
        if status not in MEMORY_STATUSES:
            raise ValueError("invalid memory status: %s" % status)
        if (contains_memory_secret({
                    "text": text, "keys": keys, "evidence": evidence,
                    "provenance": provenance, "value": value,
                    "project": project, "scope": scope, "source": source,
                    "kind": kind, "subject": subject, "device_id": device_id,
                    "mission_id": mission_id, "attribute": attribute,
                    "conflict_key": conflict_key, "context": context})
                or contains_memory_secret("%s: %s" % (
                    _memory_admission_text(keys), _memory_admission_text(text)))
                or (attribute and value is not None and contains_memory_secret(
                    "%s: %s" % (
                        _memory_admission_text(attribute),
                        _memory_admission_text(value))))):
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory")
        kind = str(kind or "fact").strip().lower()
        subject = str(subject or "project").strip().lower()
        if kind not in MEMORY_KINDS:
            raise ValueError("invalid memory kind: %s" % kind)
        if subject not in MEMORY_SUBJECTS:
            raise ValueError("invalid memory subject: %s" % subject)
        confidence = _confidence(confidence)
        observations = max(1, int(observations or 1))
        expires_at = int(expires_at) if expires_at not in (None, "") else None
        if expires_at is not None and expires_at <= 0:
            raise ValueError("expires_at must be a positive unix timestamp")
        valid_from = _optional_timestamp(valid_from, "valid_from")
        valid_to = _optional_timestamp(valid_to, "valid_to")
        observed_at = _optional_timestamp(observed_at, "observed_at")
        if valid_from is not None and valid_to is not None and valid_to <= valid_from:
            raise ValueError("valid_to must be later than valid_from")
        device_id = _short_identity(device_id)
        mission_id = _short_identity(mission_id)
        attribute = str(attribute or "").strip()[:160]
        conflict_key = _short_identity(conflict_key)
        context_json = _context_json(context)
        counter_observations = max(0, int(counter_observations or 0))
        value_json = _json_value(value) if value is not None else ""
        source = str(source or "host")
        evidence = _metadata_text(evidence)
        provenance = _metadata_text(provenance)
        scope = str(scope or project)
        # EXTRACTION: distil noisy/raw input into a clean atomic fact before storing
        # (Mem0/A-MEM lesson — raw turns retrieve worse than distilled facts). Opt-in.
        if self.distiller and _distill:
            try:
                d = self.distiller(text, keys)
                if d is None:
                    return -1          # distiller judged it not worth storing (chit-chat)
                text = d
            except Exception:
                pass
        if (contains_memory_secret({
                    "text": text, "keys": keys, "evidence": evidence,
                    "provenance": provenance, "value": value_json,
                    "project": project, "scope": scope, "source": source,
                    "kind": kind, "subject": subject, "device_id": device_id,
                    "mission_id": mission_id, "attribute": attribute,
                    "conflict_key": conflict_key, "context": context_json})
                or contains_memory_secret("%s: %s" % (
                    _memory_admission_text(keys), _memory_admission_text(text)))
                or (attribute and value_json and contains_memory_secret(
                    "%s: %s" % (attribute, value_json)))):
            # A configurable distiller is code outside this trust boundary.  A
            # second admission check prevents it from introducing a credential
            # after the raw proposal was accepted.
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory")
        vec = self.embedder.embed(text + " " + keys, kind="passage") if self.embedder else []
        # A proposal must not supersede anything before review. If proposal B replaced A and B
        # were later rejected, a verified A would remain hidden behind the rejected row forever.
        # Accepted host writes may still consolidate within the accepted set.
        near_id, sim = self._nearest(
            vec, project, RECALLABLE_STATUSES, embed_model=self.embed_model,
            scope=scope) \
            if (consolidate and vec and status in RECALLABLE_STATUSES) else (None, 0.0)
        emb = json.dumps(vec)
        cur = self.db.execute(
            """INSERT INTO facts(project,text,keys,importance,access_count,
                 last_access,created_at,embed_model,embedding,status,source,
                 evidence,provenance,scope,kind,subject,confidence,observations,
                 expires_at,device_id,mission_id,attribute,value_json,
                 valid_from,valid_to,observed_at,conflict_key,context_json,counter_observations)
               VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            # created_at defaults to now; importers pass the SOURCE's original timestamp so
            # recency weighting sees when the fact was true, not when it was migrated.
            (project, text, keys, importance, _now(), int(created_at or _now()),
             self.embed_model, emb, status, source, evidence, provenance, scope,
             kind, subject, confidence, observations, expires_at, device_id, mission_id,
             attribute, value_json, valid_from, valid_to,
             observed_at if observed_at is not None else int(created_at or _now()), conflict_key,
             context_json, counter_observations))
        rid = cur.lastrowid
        # CONSOLIDATION: a near-identical prior fact is superseded by this one, so recall
        # doesn't accumulate duplicates (keeps the newer wording; supersession already
        # filters superseded rows out of _sparse/_dense).
        # HashEmbedding is bag-of-words: two DISTINCT facts with high token overlap ("deploy prod
        # Friday" vs "…Monday") score ~1.0 and would falsely supersede each other. Require
        # near-identical similarity before merging under a weak embedder.
        eff_dedup = max(dedup_at, 0.985) if str(self.embed_model).startswith("hash") else dedup_at
        if consolidate and near_id and sim >= eff_dedup:
            self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (rid, near_id))
        if self.has_fts:
            self.db.execute("INSERT INTO facts_fts(rowid,text,keys) VALUES(?,?,?)",
                            (rid, text, keys))
        if _commit:
            self.db.commit()
        return rid

    def propose(self, text: str, keys: str = "", project: str = "global",
                source: str = "agent", evidence: str = "", provenance: str = "",
                scope: str | None = None, **kwargs) -> int:
        """Create a non-recallable claim for later host review."""
        try:
            return self.remember(text, keys=keys, project=project, status="proposed",
                                 source=source, evidence=evidence, provenance=provenance,
                                 scope=scope, **kwargs)
        except MemorySecretRejected:
            # Model-facing callers use the existing non-write result and never
            # receive the rejected value back through an error or transcript.
            return -1

    def set_preference(self, attribute: str, value, *, project: str = "global",
                       scope: str | None = None, subject: str = "owner", device_id: str = "",
                       source: str = "local_user", evidence="", provenance="",
                       created_at: int | None = None, context: dict | None = None) -> int:
        """Store an explicit, structured preference that may safely influence routing.

        This is a host-only seam: an agent-facing tool still creates ``proposed`` claims.  A new
        explicit value supersedes only earlier explicit preferences for the same boundary; verified
        habits remain underneath it and become visible again if the user later invalidates the
        preference.
        """
        attribute = str(attribute or "").strip()[:160]
        if not attribute:
            raise ValueError("preference attribute is required")
        if (contains_memory_secret({
                    "attribute": attribute, "value": value, "project": project,
                    "scope": scope, "subject": subject, "device_id": device_id,
                    "source": source, "evidence": evidence,
                    "provenance": provenance})
                or contains_memory_secret("%s: %s" % (
                    _memory_admission_text(attribute), _memory_admission_text(value)))):
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory")
        project = str(project or "global")
        scope = str(scope or project)
        subject = str(subject or "owner").strip().lower()
        if subject not in MEMORY_SUBJECTS:
            raise ValueError("invalid memory subject: %s" % subject)
        device_id = _short_identity(device_id)
        context_json = _context_json(context)
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()[:16]
        conflict_key = "preference:%s:%s:%s:%s" % (
            subject, attribute, device_id or "all", context_hash)
        text = "%s = %s" % (attribute, _display_value(value))
        try:
            self.db.execute("BEGIN IMMEDIATE")
            rid = self.remember(
                text, keys=attribute, project=project, status="attested", source=source,
                evidence=evidence, provenance=provenance, scope=scope, kind="preference",
                subject=subject, confidence=1.0, observations=1, device_id=device_id,
                attribute=attribute, value=value, consolidate=False, created_at=created_at,
                context=context, conflict_key=conflict_key,
                _commit=False, _distill=False)
            self.db.execute(
                """UPDATE facts SET superseded_by=?
                   WHERE id<>? AND project=? AND scope=? AND kind='preference'
                     AND subject=? AND attribute=? AND device_id=?
                     AND context_json=?
                     AND superseded_by IS NULL AND status IN ('active','attested','verified')""",
                (rid, rid, project, scope, subject, attribute, device_id, context_json))
            self.db.commit()
            return rid
        except Exception:
            self.db.rollback()
            raise

    def record_habit_observation(self, attribute: str, value, *, project: str = "global",
                                 scope: str | None = None, subject: str = "owner",
                                 device_id: str = "", source: str = "host_observation",
                                 evidence="", provenance="", observed_at: int | None = None,
                                 verify_after: int = 3, context: dict | None = None) -> int:
        """Record a deterministic user choice and verify the habit only after repetition.

        The first observations stay quarantined.  Once ``verify_after`` matching choices have been
        observed, the host may use the habit for automatic routing.  Model-inferred guesses never
        call this seam and remain ordinary proposals.
        """
        attribute = str(attribute or "").strip()[:160]
        if not attribute:
            raise ValueError("habit attribute is required")
        if (contains_memory_secret({
                    "attribute": attribute, "value": value, "project": project,
                    "scope": scope, "subject": subject, "device_id": device_id,
                    "source": source, "evidence": evidence,
                    "provenance": provenance})
                or contains_memory_secret("%s: %s" % (
                    _memory_admission_text(attribute), _memory_admission_text(value)))):
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory")
        verify_after = max(2, min(20, int(verify_after or 3)))
        project = str(project or "global")
        scope = str(scope or project)
        subject = str(subject or "owner").strip().lower()
        if subject not in MEMORY_SUBJECTS:
            raise ValueError("invalid memory subject: %s" % subject)
        device_id = _short_identity(device_id)
        encoded = _json_value(value)
        context_json = _context_json(context)
        now = int(observed_at or _now())
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                """SELECT id,observations,status FROM facts
                   WHERE project=? AND scope=? AND kind='habit' AND subject=?
                     AND attribute=? AND value_json=? AND device_id=? AND context_json=?
                     AND superseded_by IS NULL AND status IN ('proposed','verified')
                   ORDER BY id DESC LIMIT 1""",
                (project, scope, subject, attribute, encoded, device_id, context_json)).fetchone()
            if row:
                count = int(row["observations"] or 1) + 1
                status = "verified" if count >= verify_after else "proposed"
                confidence = min(0.95, 0.35 + (0.15 * count))
                self.db.execute(
                    """UPDATE facts SET observations=?,confidence=?,status=?,last_access=?,
                              evidence=?,provenance=?,review_source=?,review_evidence=?,
                              review_provenance=?,reviewed_at=? WHERE id=?""",
                    (count, confidence, status, now, _metadata_text(evidence),
                     _metadata_text(provenance), source if status == "verified" else "",
                     _metadata_text(evidence) if status == "verified" else "",
                     _metadata_text(provenance) if status == "verified" else "",
                     now if status == "verified" else None, int(row["id"])))
                rid = int(row["id"])
            else:
                rid = self.remember(
                    "%s = %s" % (attribute, _display_value(value)), keys=attribute,
                    project=project, status="proposed", source=source, evidence=evidence,
                    provenance=provenance, scope=scope, kind="habit", subject=subject,
                    confidence=0.5, observations=1, device_id=device_id,
                    attribute=attribute, value=value, consolidate=False, created_at=now,
                    context=context,
                    _commit=False, _distill=False)
            self.db.commit()
            return rid
        except Exception:
            self.db.rollback()
            raise

    def trusted_profile(self, project: str = "global", *, device_id: str = "",
                        min_confidence: float = 0.7, allowed_scopes=None) -> dict[str, dict]:
        """Return routing-safe preferences/habits with provenance and deterministic precedence.

        Explicit preferences beat habits; device-specific entries beat general ones; project-local
        entries beat global entries.  Proposed, expired, rejected and low-confidence rows never
        participate.  The returned metadata lets a receipt explain *why* Collie chose a route.
        """
        project = str(project or "global")
        device_id = _short_identity(device_id)
        min_confidence = _confidence(min_confidence)
        projects, scopes, legacy = self._read_boundary(project, allowed_scopes)
        if not scopes:
            return {}
        q = ",".join("?" * len(scopes))
        pq = ",".join("?" * len(projects))
        now = _now()
        rows = self.db.execute(
            """SELECT id,project,scope,kind,subject,attribute,value_json,text,status,
                      confidence,observations,source,evidence,provenance,review_source,
                      review_evidence,review_provenance,reviewed_at,created_at,device_id,
                      valid_from,valid_to,observed_at,conflict_key
               FROM facts
               WHERE project IN (%s) AND scope IN (%s)
                 AND kind IN ('preference','habit')
                 AND status IN ('attested','verified') AND confidence>=?
                 AND context_json='{}'
                 AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at>?)
                 AND created_at<=? AND (valid_from IS NULL OR valid_from<=?)
                 AND (valid_to IS NULL OR valid_to>?)
                 AND (device_id='' OR device_id=?)""" % (pq, q),
            (*projects, *scopes, min_confidence, now, now, now, now, device_id)).fetchall()

        def priority(row):
            return (
                1 if row["kind"] == "preference" else 0,
                1 if device_id and row["device_id"] == device_id else 0,
                1 if row["project"] in (project, legacy) else 0,
                float(row["confidence"] or 0),
                int(row["reviewed_at"] or row["created_at"] or 0),
                int(row["id"]),
            )

        out = {}
        for row in sorted(rows, key=priority, reverse=True):
            key = str(row["attribute"] or "").strip()
            if not key or key in out:
                continue
            try:
                value = json.loads(row["value_json"])
            except Exception:
                value = row["text"]
            item = dict(row)
            item["value"] = value
            item.pop("value_json", None)
            out[key] = item
        return out

    def promote(self, memory_id: int, status: str = "active", *, evidence=None,
                source=None, provenance=None, scope=None, review_source=None,
                review_provenance=None, reviewed_at: int | None = None,
                consolidate: bool = True, dedup_at: float = 0.93) -> bool:
        """Promote one proposal into a recallable state.

        Only a proposal can be promoted.  A rejected claim is terminal: a host
        that later receives better evidence should create a fresh proposal so
        the audit history remains unambiguous.
        """
        if status not in RECALLABLE_STATUSES:
            raise ValueError("promotion status must be active, attested, or verified")
        # ``source``/``provenance`` are accepted as ergonomic aliases for the
        # reviewer metadata, never as permission to rewrite who produced the
        # claim.  Producer provenance is immutable after INSERT.
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        if contains_memory_secret({
                "evidence": evidence, "review_source": reviewer,
                "review_provenance": reviewer_provenance}):
            return False
        changes = ["status=?", "review_source=?", "review_provenance=?",
                   "reviewed_at=?", "superseded_by=NULL"]
        params = [status, _metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        memory_id = int(memory_id)
        params.append(memory_id)
        try:
            # Serialize accepted-set selection and status transition.  No fact
            # is superseded unless this exact proposal successfully promotes in
            # the same transaction.
            self.db.execute("BEGIN IMMEDIATE")
            proposal = self.db.execute(
                """SELECT project,scope,embedding,embed_model,kind,confidence FROM facts
                   WHERE id=? AND status='proposed'""", (memory_id,)).fetchone()
            if proposal is None:
                self.db.rollback()
                return False
            # ``scope`` remains in the public signature as a compatibility
            # assertion, but a reviewer cannot widen or rewrite the producer's
            # trust boundary.  A mismatch fails closed and leaves the proposal
            # pending for an explicitly authorized review path.
            if (scope is not None
                    and str(scope or proposal["project"]) != str(proposal["scope"])):
                self.db.rollback()
                return False
            # An explicit local attestation is stronger than an observed/inferred confidence score.
            # Without this, a reviewed profile proposal could become `attested` yet remain below the
            # routing-safe threshold forever, which makes the review control lie about its effect.
            if status == "attested" and proposal["kind"] in PROFILE_KINDS:
                changes.append("confidence=?")
                params.insert(-1, 1.0)
            near_id, sim = None, 0.0
            try:
                vec = json.loads(proposal["embedding"] or "[]")
            except Exception:
                vec = []
            if consolidate and vec:
                near_id, sim = self._nearest(
                    vec, proposal["project"], RECALLABLE_STATUSES,
                    embed_model=proposal["embed_model"], exclude_id=memory_id,
                    scope=proposal["scope"])
            cur = self.db.execute(
                "UPDATE facts SET %s WHERE id=? AND status='proposed'" % ", ".join(changes),
                params)
            if cur.rowcount != 1:
                self.db.rollback()
                return False
            model = str(proposal["embed_model"] or "")
            threshold = max(float(dedup_at), 0.985) if model.startswith("hash") \
                else float(dedup_at)
            if near_id and sim >= threshold:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?",
                                (memory_id, near_id))
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def reject(self, memory_id: int, *, evidence=None, source=None,
               provenance=None, review_source=None, review_provenance=None,
               reviewed_at: int | None = None) -> bool:
        """Reject one proposal without deleting its provenance/audit record."""
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        if contains_memory_secret({
                "evidence": evidence, "review_source": reviewer,
                "review_provenance": reviewer_provenance}):
            return False
        changes = ["status='rejected'", "review_source=?", "review_provenance=?",
                   "reviewed_at=?"]
        params = [_metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        params.append(int(memory_id))
        cur = self.db.execute(
            "UPDATE facts SET %s WHERE id=? AND status='proposed'" % ", ".join(changes),
            params)
        self.db.commit()
        return cur.rowcount == 1

    def invalidate(self, memory_id: int, *, evidence=None, source=None,
                   provenance=None, review_source=None, review_provenance=None,
                   reviewed_at: int | None = None) -> bool:
        """Remove an accepted claim from recall while retaining its audit record."""
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        if contains_memory_secret({
                "evidence": evidence, "review_source": reviewer,
                "review_provenance": reviewer_provenance}):
            return False
        changes = ["status='invalidated'", "review_source=?", "review_provenance=?",
                   "reviewed_at=?"]
        params = [_metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        params.append(int(memory_id))
        accepted = ",".join("?" * len(RECALLABLE_STATUSES))
        params.extend(tuple(RECALLABLE_STATUSES))
        cur = self.db.execute(
            "UPDATE facts SET %s WHERE id=? AND status IN (%s)" %
            (", ".join(changes), accepted), params)
        if cur.rowcount:
            # If an older accepted fact was consolidated under this now-invalid
            # row, revive it.  Invalidating the newest claim must not erase the
            # last known-good memory.
            self.db.execute("UPDATE facts SET superseded_by=NULL WHERE superseded_by=?",
                            (int(memory_id),))
        self.db.commit()
        return cur.rowcount == 1

    # Verbose aliases make the lifecycle API self-documenting for host layers;
    # the short forms above remain convenient for direct use and tests.
    promote_memory = promote
    reject_memory = reject
    invalidate_memory = invalidate

    def get_claim(self, memory_id: int) -> dict | None:
        row = self.db.execute(
            """SELECT id,project,text,keys,importance,created_at,superseded_by,
                      status,source,evidence,provenance,scope,review_source,
                      review_evidence,review_provenance,reviewed_at,kind,subject,
                      confidence,observations,expires_at,device_id,mission_id,
                      attribute,value_json,valid_from,valid_to,observed_at,conflict_key,
                      claim_id,revision,origin_device,updated_at,deleted_at,
                      supersedes_claim_id,evidence_ids_json
                      ,context_json,counter_observations,relations_json
               FROM facts WHERE id=?""", (int(memory_id),)).fetchone()
        if not row:
            return None
        out = dict(row)
        if out.get("value_json"):
            try:
                out["value"] = json.loads(out["value_json"])
            except Exception:
                out["value"] = None
        out["context"] = _context_value(out.get("context_json"))
        out["evidence_ids"] = _evidence_ids(out.get("evidence_ids_json"))
        out["relations"] = _relation_values(out.get("relations_json"))
        return out

    def list_claims(self, status: str | None = None, project: str | None = None,
                    limit: int = 100, *, allowed_scopes=None, device_id=None,
                    root_scope_only: bool = False) -> list[dict]:
        """Review surface for hosts; rejected/proposed claims stay out of recall."""
        where, params = [], []
        if status is not None:
            if status not in MEMORY_STATUSES:
                raise ValueError("invalid memory status: %s" % status)
            where.append("status=?")
            params.append(status)
        boundary_scopes = None
        if project is not None:
            projects, boundary_scopes, _legacy = self._read_boundary(
                str(project or "global"), allowed_scopes, include_global=False)
            where.append("project IN (%s)" % ",".join("?" * len(projects)))
            params.extend(projects)
        # An unscoped all-project listing is the existing local-admin review
        # surface (``collie mem pending``).  Once a project or explicit scope
        # capability is supplied, however, list obeys the same trust boundary
        # as recall.
        scopes = None
        if project is not None or allowed_scopes is not None:
            scopes = (boundary_scopes if boundary_scopes is not None else
                      self._allowed_scopes(project or "global", allowed_scopes))
            if not scopes:
                return []
            where.append("scope IN (%s)" % ",".join("?" * len(scopes)))
            params.extend(scopes)
        if device_id is not None:
            where.append("(COALESCE(device_id,'')='' OR device_id=?)")
            params.append(str(device_id))
        if root_scope_only:
            # Host-level review may see only project/global claims. Mission-specific rows require
            # the exact Mission review capability and must not consume this query's LIMIT.
            where.append("scope=project")
            where.append("COALESCE(mission_id,'')=''")
        sql = "SELECT * FROM facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(1000, int(limit))))
        out = []
        for row in self.db.execute(sql, params).fetchall():
            item = dict(row)
            if item.get("value_json"):
                try:
                    item["value"] = json.loads(item["value_json"])
                except (TypeError, ValueError):
                    item["value"] = None
            item["context"] = _context_value(item.get("context_json"))
            item["evidence_ids"] = _evidence_ids(item.get("evidence_ids_json"))
            item["relations"] = _relation_values(item.get("relations_json"))
            out.append(item)
        return out

    def rebuild_fts(self) -> int:
        """Repopulate the FTS index from facts (recovery after index corruption or an
        index-time transform experiment; external-content table indexes what WE insert)."""
        if not self.has_fts:
            return 0
        self.db.execute("INSERT INTO facts_fts(facts_fts) VALUES('delete-all')")
        rows = self.db.execute("SELECT id,text,keys FROM facts").fetchall()
        for r in rows:
            self.db.execute("INSERT INTO facts_fts(rowid,text,keys) VALUES(?,?,?)",
                            (r["id"], r["text"] or "", r["keys"] or ""))
        self.db.commit()
        return len(rows)

    def reembed_all(self) -> int:
        """Re-embed every fact with the current embedder (after a model swap).
        Embeddings from different models live in different spaces, so a switch
        requires this pass; store `embed_model` so we know what's stale."""
        if self.embedder is None:                          # BM25-only: nothing to (re)embed
            return 0
        rows = self.db.execute("SELECT id, text, keys FROM facts").fetchall()
        for r in rows:
            emb = json.dumps(self.embedder.embed(
                (r["text"] or "") + " " + (r["keys"] or ""), kind="passage"))
            self.db.execute("UPDATE facts SET embedding=?, embed_model=? WHERE id=?",
                            (emb, self.embed_model, r["id"]))
        self.db.commit()
        return len(rows)

    def count(self, project: str | None = None) -> int:
        now = _now()
        if project:
            projects, _scopes, _legacy = self._read_boundary(
                str(project), include_global=False)
            return self.db.execute(
                """SELECT COUNT(*) FROM facts WHERE project IN (%s)
                   AND superseded_by IS NULL
                   AND (expires_at IS NULL OR expires_at>?) AND created_at<=?
                   AND (valid_from IS NULL OR valid_from<=?)
                   AND (valid_to IS NULL OR valid_to>?)""" %
                ",".join("?" * len(projects)),
                (*projects, now, now, now, now)).fetchone()[0]
        return self.db.execute(
            """SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL
               AND (expires_at IS NULL OR expires_at>?) AND created_at<=?
               AND (valid_from IS NULL OR valid_from<=?)
               AND (valid_to IS NULL OR valid_to>?)""",
            (now, now, now, now)).fetchone()[0]

    # ------------------------------------------------------------------ #
    #  ARCHIVAL read — HYBRID retrieval (the pain-#1 fix)
    # ------------------------------------------------------------------ #
    def _sparse(self, query: str, project: str, limit: int,
                statuses=None, *, allowed_scopes=None,
                device_id: str = "", as_of: int | None = None,
                known_at: int | None = None) -> list[tuple[int, float]]:
        device_id = _short_identity(device_id)
        as_of = _optional_timestamp(as_of, "as_of") or _now()
        known_at = _optional_timestamp(known_at, "known_at") or _now()
        statuses = self._statuses(statuses)
        projects, scopes, _legacy = self._read_boundary(project, allowed_scopes)
        if not scopes:
            return []
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        project_q = ",".join("?" * len(projects))
        rows = []
        if self.has_fts:
            try:
                match = " OR ".join(_fts_terms(query)) or query
                rows = self.db.execute(
                    """SELECT f.id, bm25(facts_fts) AS score
                       FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                       WHERE facts_fts MATCH ? AND f.project IN (%s)
                             AND (f.device_id='' OR f.device_id=?)
                             AND (f.expires_at IS NULL OR f.expires_at>?)
                             AND f.created_at<=?
                             AND (f.valid_from IS NULL OR f.valid_from<=?)
                             AND (f.valid_to IS NULL OR f.valid_to>?)
                             AND f.superseded_by IS NULL AND f.status IN (%s)
                             AND f.scope IN (%s)
                       ORDER BY score LIMIT ?""" % (project_q, sq, scope_q),
                    (match, *projects, device_id, known_at, known_at, as_of, as_of,
                     *statuses, *scopes,
                     limit)).fetchall()
                # bm25 lower == better; return as (id, rank_score) ascending handled by RRF
                return [(r["id"], -r["score"]) for r in rows]
            except sqlite3.OperationalError:
                pass
        # LIKE fallback: match ANY of the first few query tokens (not just the first word)
        toks = [t for t in query.strip().split() if len(t) > 2][:4] or [query.strip()]
        clause = " OR ".join(["text LIKE ? OR keys LIKE ?"] * len(toks))
        params = [*projects, device_id, known_at, known_at, as_of, as_of,
                  *statuses, *scopes]
        for t in toks:
            params += ["%" + t + "%", "%" + t + "%"]
        params.append(limit)
        rows = self.db.execute(
            "SELECT id FROM facts WHERE project IN (%s) " % project_q +
            "AND (device_id='' OR device_id=?) "
            "AND (expires_at IS NULL OR expires_at>?) AND created_at<=? "
            "AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR valid_to>?) AND status IN (%s) "
            "AND superseded_by IS NULL AND scope IN (%s) "
            "AND (%s) LIMIT ?" % (sq, scope_q, clause), params).fetchall()
        return [(r["id"], 1.0) for r in rows]

    def _dense(self, query: str, project: str, limit: int,
               statuses=None, *, allowed_scopes=None,
               device_id: str = "", as_of: int | None = None,
               known_at: int | None = None) -> list[tuple[int, float]]:
        if self.embedder is None:                          # BM25-only mode — no dense arm
            return []
        statuses = self._statuses(statuses)
        device_id = _short_identity(device_id)
        as_of = _optional_timestamp(as_of, "as_of") or _now()
        known_at = _optional_timestamp(known_at, "known_at") or _now()
        projects, scopes, _legacy = self._read_boundary(project, allowed_scopes)
        if not scopes:
            return []
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        project_q = ",".join("?" * len(projects))
        qv = self.embedder.embed(query, kind="query")
        rows = self.db.execute(
            "SELECT id, embedding FROM facts WHERE project IN (%s) " % project_q +
            "AND (device_id='' OR device_id=?) "
            "AND (expires_at IS NULL OR expires_at>?) "
            "AND created_at<=? AND (valid_from IS NULL OR valid_from<=?) "
            "AND (valid_to IS NULL OR valid_to>?) "
            "AND superseded_by IS NULL AND status IN (%s) AND scope IN (%s)" %
            (sq, scope_q), (*projects, device_id, known_at, known_at, as_of, as_of,
                            *statuses, *scopes)).fetchall()
        # HashEmbedding (bag-of-words) produces spurious positive cosines on token overlap, so we
        # abstain on non-positive for it; a REAL semantic embedder's weakly-related passage (cosine
        # near 0) is genuine signal — keep it so cross-lingual/paraphrase matches enter RRF.
        floor = 0.0 if str(self.embed_model).startswith("hash") else -1.0
        scored = []
        for r in rows:
            try:
                s = cosine(qv, json.loads(r["embedding"]))
                if s > floor:
                    scored.append((r["id"], s))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def recall(self, query: str, project: str = "global", k: int = 8,
               pool: int = 50, statuses=None, *, allowed_scopes=None,
               device_id: str = "", as_of: int | None = None,
               known_at: int | None = None, graph_entities=None,
               graph_hops: int = 0) -> list[dict]:
        """Time-aware hybrid recall; graph expansion is opt-in for entity/multi-hop queries."""
        project = str(project or "global")
        device_id = _short_identity(device_id)
        as_of = _optional_timestamp(as_of, "as_of") or _now()
        known_at = _optional_timestamp(known_at, "known_at") or _now()
        statuses = self._statuses(statuses)
        projects, scopes, _legacy = self._read_boundary(project, allowed_scopes)
        if not scopes:
            return []
        sparse = self._sparse(
            query, project, pool, statuses, allowed_scopes=scopes,
            device_id=device_id, as_of=as_of, known_at=known_at)
        dense = self._dense(
            query, project, pool, statuses, allowed_scopes=scopes,
            device_id=device_id, as_of=as_of, known_at=known_at)
        rank_lists = [[i for i, _ in sparse], [i for i, _ in dense]]
        if graph_entities and int(graph_hops or 0) > 0:
            graph = self.graph_expand(
                graph_entities, project=project, allowed_scopes=scopes,
                device_id=device_id, as_of=as_of, known_at=known_at,
                max_hops=graph_hops)
            rank_lists.append([item["claim_id"] for item in graph])
        fused = rrf(rank_lists, k=60)
        # With a reranker, fuse to a LARGER candidate pool and let the cross-encoder pick
        # the final top-k (it scores query+doc jointly — sharper than RRF's rank fusion).
        cand = fused[: max(k, 24)]                       # headroom for rerank/recency re-ordering; A/B'd 2026-07-17: pools of 36/50 LOWER strict@10 (59%/55% vs 62%) — deeper candidates only feed the cross-encoder topically-close-but-answerless distractors
        if not cand:
            return []
        q = ",".join("?" * len(cand))
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        project_q = ",".join("?" * len(projects))
        rows = self.db.execute(
            """SELECT id,text,keys,importance,created_at,status,source,evidence,
                      provenance,scope,review_source,review_evidence,
                      review_provenance,reviewed_at,kind,subject,confidence,
                      observations,expires_at,device_id,mission_id,attribute,value_json,
                      valid_from,valid_to,observed_at,conflict_key,claim_id,revision,
                      origin_device,updated_at,supersedes_claim_id,evidence_ids_json
                      ,context_json,counter_observations,relations_json
                      FROM facts WHERE id IN (%s)
                      AND project IN (%s)
                      AND (device_id='' OR device_id=?)
                      AND (expires_at IS NULL OR expires_at>?)
                      AND created_at<=?
                      AND (valid_from IS NULL OR valid_from<=?)
                      AND (valid_to IS NULL OR valid_to>?)
                      AND superseded_by IS NULL AND status IN (%s)
                      AND scope IN (%s)""" % (q, project_q, sq, scope_q),
            [i for i, _ in cand] + [*projects, device_id, known_at, known_at,
                                    as_of, as_of,
                                    *statuses, *scopes]).fetchall()
        by_id = {r["id"]: r for r in rows}

        # A conflict key is one logical property over time.  If imperfect imported data leaves
        # overlapping open intervals, choose the latest admissible version for this query while
        # retaining every version for historical/audit reads.
        winners = {}
        for row in rows:
            key = str(row["conflict_key"] or "")
            if not key:
                continue
            stamp = int(row["valid_from"] or row["observed_at"] or row["created_at"] or 0)
            prior = winners.get(key)
            prior_stamp = (int(prior["valid_from"] or prior["observed_at"] or
                               prior["created_at"] or 0) if prior else -1)
            if prior is None or (stamp, int(row["id"])) > (prior_stamp, int(prior["id"])):
                winners[key] = row
        winning_ids = {int(row["id"]) for row in winners.values()}
        for row in tuple(rows):
            if row["conflict_key"] and int(row["id"]) not in winning_ids:
                by_id.pop(int(row["id"]), None)

        ranked = [(rid, score) for rid, score in cand if rid in by_id]  # default: RRF order
        if self.reranker:
            ids = [rid for rid, _ in cand if rid in by_id]
            docs = [(by_id[rid]["text"] or "") + " " + (by_id[rid]["keys"] or "")
                    for rid in ids]
            try:
                scores = self.reranker.rerank(query, docs)
                ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
            except Exception:
                pass                                     # cross-encoder failed -> keep RRF

        # RECENCY: newer facts are more likely to describe the CURRENT state (ports move,
        # decisions get reversed), so give them a mild multiplicative edge — relevance still
        # dominates. Scores are rebuilt on the rank scale (1/(60+pos)) so the same rule works
        # over RRF and cross-encoder output alike; a uniform-age corpus (evals) multiplies
        # every row by the same factor and keeps its order. Half-life in days via Settings
        # (RECENCY_HALFLIFE, default 90); 0 disables.
        try:
            from . import settings as _settings
            half = float(_settings.get("RECENCY_HALFLIFE", "90") or 0)
        except Exception:
            half = 90.0
        if half > 0:
            now = as_of
            rescored = []
            for pos, (rid, _s) in enumerate(ranked):
                r = by_id.get(rid)
                effective_stamp = ((r["valid_from"] or r["observed_at"] or r["created_at"])
                                   if r else now)
                age_days = max(0, now - (effective_stamp or now)) / 86400.0
                boost = 1.0 + 0.5 * (0.5 ** (age_days / half))
                # Multiply the ACTUAL fused/reranker relevance score by the recency boost (≤1.5x), so
                # relevance keeps dominating and margins are preserved. Rebuilding on pure rank position
                # (1/(60+pos)) threw away the relevance gaps, letting a fresh low-relevance distractor
                # leapfrog the true top hit — and, since top-k truncation runs after this, evict it.
                rescored.append((rid, float(_s) * boost))
            ranked = sorted(rescored, key=lambda x: x[1], reverse=True)
        ranked = ranked[:k]

        out = []
        for rid, score in ranked:
            r = by_id.get(rid)
            if r:
                out.append({"id": rid, "text": r["text"], "keys": r["keys"],
                            "score": round(float(score), 4), "status": r["status"],
                            "source": r["source"], "evidence": r["evidence"],
                            "provenance": r["provenance"], "scope": r["scope"],
                            "review_source": r["review_source"],
                            "review_evidence": r["review_evidence"],
                            "review_provenance": r["review_provenance"],
                            "reviewed_at": r["reviewed_at"], "kind": r["kind"],
                            "subject": r["subject"], "confidence": r["confidence"],
                            "observations": r["observations"],
                            "expires_at": r["expires_at"], "device_id": r["device_id"],
                            "mission_id": r["mission_id"], "attribute": r["attribute"],
                            "valid_from": r["valid_from"], "valid_to": r["valid_to"],
                            "observed_at": r["observed_at"],
                            "conflict_key": r["conflict_key"], "claim_id": r["claim_id"],
                            "revision": r["revision"], "origin_device": r["origin_device"],
                            "updated_at": r["updated_at"],
                            "supersedes_claim_id": r["supersedes_claim_id"],
                            "evidence_ids": _evidence_ids(r["evidence_ids_json"]),
                            "context": _context_value(r["context_json"]),
                            "counter_observations": r["counter_observations"],
                            "relations": _relation_values(r["relations_json"])})
                self.db.execute(
                    "UPDATE facts SET access_count=access_count+1, last_access=? WHERE id=?",
                    (_now(), rid))
        self.db.commit()
        return out

    def set_claim_relations(self, memory_id: int, relations, **kwargs) -> int:
        """Replace the retractable derived graph edges supported by one accepted claim."""
        from .memory_graph import MemoryGraph
        return MemoryGraph(self).set_claim_relations(memory_id, relations, **kwargs)

    def graph_expand(self, entities, *, project: str = "global", allowed_scopes=None,
                     device_id: str = "", as_of: int | None = None,
                     known_at: int | None = None,
                     max_hops: int = 3, max_nodes: int = 100) -> list[dict]:
        """Return graph-supported claim IDs; callers must explicitly opt into graph retrieval."""
        from .memory_graph import MemoryGraph
        return MemoryGraph(self).expand(
            entities, project=project, allowed_scopes=allowed_scopes,
            device_id=device_id, as_of=as_of, known_at=known_at,
            max_hops=max_hops, max_nodes=max_nodes)

    def memory_sync(self):
        from .memory_sync import MemorySync
        return MemorySync(self)

    def evidence_store(self):
        from .memory_evidence import MemoryEvidence
        return MemoryEvidence(self)

    def preference_resolver(self):
        from .memory_preferences import PreferenceResolver
        return PreferenceResolver(self)

    def resolve_preference(self, attribute: str, **kwargs) -> dict:
        return self.preference_resolver().resolve(attribute, **kwargs)

    def erase_claim(self, memory_id: int) -> bool:
        """Physically erase a claim locally and emit a replication tombstone."""
        row = self.db.execute("SELECT * FROM facts WHERE id=?", (int(memory_id),)).fetchone()
        if not row:
            return False
        evidence_ids = [item["evidence_id"] for item in self.db.execute(
            "SELECT evidence_id FROM memory_claim_evidence WHERE claim_id=?",
            (row["claim_id"],)).fetchall()]
        self.db.execute("DELETE FROM memory_edges WHERE claim_id=?", (int(memory_id),))
        self.db.execute("DELETE FROM memory_graph_extractions WHERE claim_id=?", (row["claim_id"],))
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=?", (row["claim_id"],))
        for evidence_id in evidence_ids:
            self.db.execute("""DELETE FROM memory_evidence WHERE evidence_id=? AND NOT EXISTS(
                SELECT 1 FROM memory_claim_evidence WHERE evidence_id=?)""",
                (evidence_id, evidence_id))
        if self.has_fts:
            try:
                self.db.execute("""INSERT INTO facts_fts(facts_fts,rowid,text,keys)
                                   VALUES('delete',?,?,?)""",
                                (row["id"], row["text"] or "", row["keys"] or ""))
            except sqlite3.OperationalError:
                pass
        self.db.execute("DELETE FROM facts WHERE id=?", (int(memory_id),))
        from .memory_sync import _minimal_delete_payload
        minimal_json = json.dumps(_minimal_delete_payload(dict(row)), ensure_ascii=False,
                                  separators=(",", ":"))
        self.db.execute("""DELETE FROM memory_claim_changes
                           WHERE claim_id=? AND operation<>'delete'""", (row["claim_id"],))
        self.db.execute("""UPDATE memory_sync_conflicts
            SET local_payload_json=?,remote_payload_json=? WHERE claim_id=?""",
            (minimal_json, minimal_json, row["claim_id"]))
        self.db.commit()
        return True

    def close(self) -> None:
        self.db.close()


def _metadata_text(value) -> str:
    """Persist host metadata as deterministic text while accepting JSON-shaped values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("memory confidence must be a number from 0 to 1")
    if not math.isfinite(number) or number < 0 or number > 1:
        raise ValueError("memory confidence must be a number from 0 to 1")
    return number


def _optional_timestamp(value, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("%s must be a positive unix timestamp" % field)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a positive unix timestamp" % field)
    if number <= 0:
        raise ValueError("%s must be a positive unix timestamp" % field)
    return number


def _short_identity(value) -> str:
    value = str(value or "").strip()
    if len(value) > 200:
        raise ValueError("memory identity is too long")
    return value


def _json_value(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("memory profile value must be JSON-serializable")


_CONTEXT_KEYS = frozenset(("task_type", "channel", "project", "device", "audience", "urgency"))


def _context_json(value) -> str:
    if value in (None, {}):
        return "{}"
    if not isinstance(value, dict) or set(value) - _CONTEXT_KEYS:
        raise ValueError("memory context has unknown fields")
    clean = {}
    for key, item in value.items():
        if isinstance(item, (list, tuple, set)):
            values = [str(entry).strip()[:120] for entry in item if str(entry).strip()]
            if values:
                clean[key] = sorted(set(values))
        elif item not in (None, ""):
            clean[key] = str(item).strip()[:120]
    return json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _context_value(value) -> dict:
    try:
        out = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _evidence_ids(value) -> list[str]:
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [str(item) for item in rows if str(item).startswith("evi_")][:100]


def _relation_values(value) -> list[dict]:
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)][:100]


def _display_value(value) -> str:
    if isinstance(value, str):
        return value[:300]
    return _json_value(value)[:300]


def _fts_terms(query: str) -> list[str]:
    """Sanitize a free-text query into safe FTS5 terms (avoids syntax errors).
    NOTE unicode61 keeps a CJK run as ONE token, so the sparse leg only matches Chinese
    on identical runs — a bigram index+query expansion was A/B'd 2026-07-17 and did NOT
    help (strict@10 59% vs 62% baseline on 29 real queries; the dense leg already carries
    Chinese, extra bigram candidates only displaced strict hits). Revisit only if
    Chinese-keyword misses show up in practice."""
    import re
    toks = re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", query)
    return ['"%s"' % t for t in toks if len(t) > 1][:12]


def rrf(rank_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. rank_lists = list of id-lists ordered best-first."""
    scores: dict[int, float] = {}
    for lst in rank_lists:
        for rank, rid in enumerate(lst):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
