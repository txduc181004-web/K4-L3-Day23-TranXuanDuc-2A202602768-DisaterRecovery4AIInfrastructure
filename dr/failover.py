"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của region; không raise — region không trả lời thì reachable=False."""
    try:
        r = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
        return {"reachable": True, **r.json()}
    except Exception as e:
        return {"region": region, "reachable": False, "error": type(e).__name__}


def is_ready(region: str) -> tuple[bool, list]:
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
        return r.status_code == 200, r.json().get("reasons", [])
    except Exception as e:
        return False, [type(e).__name__]


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước verify -> restore -> scale -> wait_ready -> cutover, đúng thứ tự."""
    primary = "a" if target == "b" else "b"
    t_start = time.time()
    res = {"ok": False, "target": target, "primary": primary, "backend": backend}

    def abort(step, why, **kw):
        res.update(aborted_at=step, reason=why, elapsed_s=round(time.time() - t_start, 2))
        emit(step=step, target=target, ok=False, aborted=True, reason=why, **kw)
        return res

    # 1. Region phụ hiện có gì? Process phải trả lời được thì mới restore vào nó.
    before = state_of(target)
    res["state_before"] = before
    if before.get("reachable") is False:
        return abort("1_verify_target", "target_unreachable", state=before)
    emit(step="1_verify_target", target=target, ok=True, state=before)

    # 2. Restore vector DB + weights từ snapshot, đo RPO thật (doc primary có mà b không có).
    try:
        meta = snapshot.get(target, backend)
    except BaseException as e:  # snapshot.get dùng SystemExit khi chưa có snapshot nào
        return abort("2_restore_snapshot", f"restore_failed: {e}")
    tdir = pathlib.Path(f"state/region-{target}")
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     tdir / "vectors.sqlite")
    res["restore"] = {**meta, **r}
    emit(step="2_restore_snapshot", target=target, ok=True, backend=backend,
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"),
         snapshot_age_s=round(time.time() - meta["snapshot_at"], 2)
         if meta.get("snapshot_at") else None,
         primary_latest_doc_ts=r["primary_latest_doc_ts"],
         restored_latest_doc_ts=r["restored_latest_doc_ts"])

    # 3. warm -> full: serving bắt đầu đếm GPU pool warm-up từ lúc này.
    tdir.mkdir(parents=True, exist_ok=True)
    prev = (tdir / "pool_state").read_text().strip() if (tdir / "pool_state").exists() else None
    (tdir / "pool_state").write_text("full")
    emit(step="3_scale_pool", target=target, ok=True, pool_from=prev, pool_to="full")

    # 4. Poll /readyz. Timeout -> ABORT, tuyệt đối không cutover.
    t4 = time.time()
    ready, reasons = is_ready(target)
    while not ready and time.time() - t4 < wait:
        time.sleep(0.5)
        ready, reasons = is_ready(target)
    waited = round(time.time() - t4, 2)
    res["waited_s"] = waited
    if not ready:
        return abort("4_wait_ready", f"target_not_ready_after_{wait}s",
                     waited_s=waited, last_reasons=reasons)
    emit(step="4_wait_ready", target=target, ok=True, waited_s=waited)

    # 5. Chỉ bây giờ mới đổi "DNS".
    old = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", target=target, ok=True, active_from=old, active_to=target)

    res.update(ok=True, state_after=state_of(target), cutover={"from": old, "to": target},
               elapsed_s=round(time.time() - t_start, 2))
    return res


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
