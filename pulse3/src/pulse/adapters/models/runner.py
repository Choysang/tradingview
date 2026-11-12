"""Model run orchestration: persist every analysis call to ``analysis.model_run`` and replay from stored output (spec FINAL §7).

Why: "模型重放优先复用当时保存的结构化输出与内容哈希". A run row records policy version, input manifest and its
context hash, analysis_as_of, start/finish times, the immutable structured output (a content-addressed raw lake
object, referenced by ``output_ref`` + ``output_hash``) and the reported usage. Re-running the same policy over
the same manifest REUSES the succeeded run instead of calling the provider again; ``replay`` reads the stored
bytes and verifies the hash.

Invariants: the output passes ``port.guard_output`` BEFORE it is stored (an invalid reply is a failed run with
its error code, nothing is stored as output); failed runs keep ``error_code`` and no output; the provider's
credentials never reach this module; usage unknown stays ``unknown``.
Not covered: queues/timeouts beyond the provider's own, release-stage binding (stage_id stays NULL).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

import psycopg

from pulse.adapters.lake import LakeStore
from pulse.adapters.postgres import intelligence_repo as repo

from .port import ModelProvider, ModelRequest, OutputRejected, ProviderError, canonical_bytes, guard_output, input_context_hash, sha256_hex


@dataclass(frozen=True)
class RunResult:
    model_run_id: uuid.UUID
    output: dict
    reused: bool
    policy_id: str
    policy_version: int


class ModelRunError(RuntimeError):
    def __init__(self, run_id: uuid.UUID, code: str):
        super().__init__(f"model run {run_id} failed: {code}")
        self.run_id, self.code = run_id, code


class ModelRunner:
    def __init__(self, conn: psycopg.Connection, lake: LakeStore, providers: Mapping[str, ModelProvider], *,
                 clock: Callable[[], datetime] | None = None):
        if not conn.autocommit:
            raise ValueError("ModelRunner needs an autocommit connection (explicit short transactions)")
        self.conn, self.lake, self.providers = conn, lake, dict(providers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ensure_policy(self, policy) -> None:
        with self.conn.transaction():
            repo.ensure_model_policy(self.conn, policy, self.clock())

    def run(self, req: ModelRequest) -> RunResult:
        p = req.policy
        self.ensure_policy(p)
        ctx = input_context_hash(req)
        prior = repo.find_succeeded_run(self.conn, p.model_policy_id, p.version, ctx)
        if prior is not None:
            return RunResult(prior["model_run_id"], self.replay(prior["model_run_id"]), True, p.model_policy_id, p.version)
        provider = self.providers.get(p.provider_id)
        if provider is None:
            raise ProviderError("provider_not_configured", p.provider_id)
        run_id = uuid.uuid4()
        started = self.clock()
        with self.conn.transaction():
            repo.insert_model_run(self.conn, run_id=run_id, policy_id=p.model_policy_id, version=p.version, context_hash=ctx,
                                  manifest=[m.to_doc() for m in req.manifest], analysis_as_of=req.analysis_as_of, started_at=started)
        t0 = time.monotonic()
        try:
            res = provider.run(req)
            output = guard_output(req, res.output)
        except (ProviderError, OutputRejected) as exc:
            code = exc.code if isinstance(exc, ProviderError) else "output_rejected"
            self._finish(run_id, "failed", None, None, {"latency_ms": int((time.monotonic() - t0) * 1000)}, code)
            raise ModelRunError(run_id, code) from exc
        data = canonical_bytes(output)
        ref = self.lake.put_bytes(data, owner=f"model_run:{run_id}", kind="raw", suffix=".json")
        usage = {**res.usage, "latency_ms": res.usage.get("latency_ms", int((time.monotonic() - t0) * 1000))}
        self._finish(run_id, "succeeded", ref.rel_path, ref.sha256, usage, None)
        self.lake.release(f"model_run:{run_id}")
        return RunResult(run_id, output, False, p.model_policy_id, p.version)

    def _finish(self, run_id, state, output_ref, output_hash, usage, error_code) -> None:
        with self.conn.transaction():
            repo.finish_model_run(self.conn, run_id, state=state, completed_at=self.clock(),
                                  output_ref=output_ref, output_hash=output_hash, usage=usage, error_code=error_code)

    def replay(self, run_id: uuid.UUID) -> dict:
        """Stored structured output of a succeeded run; the provider is NOT called. Hash is verified."""
        row = repo.get_model_run(self.conn, run_id)
        if row is None or row["run_state"] != "succeeded":
            raise KeyError(f"no succeeded model run {run_id}")
        data = self.lake.read_object(row["output_hash"])  # content address = sha256 of the bytes; verifies integrity
        if sha256_hex(data) != row["output_hash"]:
            raise ValueError("stored model output failed its hash check")
        return json.loads(data.decode("utf-8"))
