"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _alert(region: str, max_age: float):
    """State_change MỚI NHẤT của region trong health log, nếu là UNHEALTHY và còn mới."""
    ev = [e for e in _jsonl(HEALTH_LOG)
          if e.get("event") == "state_change" and e.get("region") == region]
    if ev and ev[-1]["to"] == "UNHEALTHY" and time.time() - ev[-1]["ts"] <= max_age:
        return ev[-1]
    return None


def confirm_outage(primary: str, alert_wait: float, interval: float, threshold: int) -> dict:
    """Không tin 1 lần fail: chờ alert của health checker (nguồn sự thật cho 'outage'),
    rồi probe lại trực tiếp. Không có health checker -> tự probe threshold lần liên tiếp."""
    t0 = time.time()
    alert = _alert(primary, max_age=300)
    while alert is None and time.time() - t0 < alert_wait:
        time.sleep(0.5)
        alert = _alert(primary, max_age=300)
    if alert is not None:
        ok, reason = hc.probe(primary, 2.0)
        return {"confirmed": not ok, "source": "health_checker_alert",
                "alert_ts": alert["ts"], "alert_reason": alert.get("reason"),
                "recheck_reason": reason, "waited_for_alert_s": round(time.time() - t0, 2)}
    probes = []
    for i in range(threshold):
        ok, reason = hc.probe(primary, 2.0)
        probes.append(reason)
        if ok:
            return {"confirmed": False, "source": "direct_probe", "probes": probes}
        if i < threshold - 1:
            time.sleep(interval)
    return {"confirmed": True, "source": "direct_probe", "probes": probes}


def golden_signals(target: str, n: int = 10) -> dict:
    lat, errors = [], 0
    with httpx.Client(timeout=3.0) as c:
        for i in range(n):
            t = time.time()
            try:
                r = c.get(f"{URL[target]}/v1/infer", params={"q": f"golden {i}"})
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            lat.append((time.time() - t) * 1000)
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(round(0.95 * len(lat))) - 1)]
    return {"requests": n, "errors": errors, "error_rate": errors / n,
            "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(p95, 1)}


def run(primary: str, target: str, backend: str, auto: bool,
        alert_wait: float = 120, wait: float = 60) -> dict:
    """7 bước runbook §4 'Region Chính Down'."""
    t_start = time.time()

    # 1. Xác nhận outage
    c = confirm_outage(primary, alert_wait, interval=5.0, threshold=3)
    step(1, "xac_nhan_outage", primary=primary, **c)
    if not c["confirmed"]:
        step(7, "post_incident", outcome="aborted_no_outage",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "outage_not_confirmed", **c}

    # 2. Mở incident + bấm giờ. t_outage lấy từ chaos log (drill) nếu có.
    kills = [e for e in _jsonl(CHAOS_LOG) if e.get("action") == "kill" and e.get("region") == primary]
    t_outage = kills[-1]["ts"] if kills else None
    now = time.time()
    step(2, "thong_bao_incident", primary=primary, target=target, severity="SEV1",
         t_outage=t_outage, t_alert=c.get("alert_ts"), t_notified=now,
         notify_delay_s=round(now - t_outage, 2) if t_outage else None)

    if not confirm(auto, f"Region {primary} DOWN. Failover sang region {target}?"):
        step(7, "post_incident", outcome="operator_declined",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "operator_declined"}

    # 3. Failover — gọi ĐÚNG 1 lần, nó tự làm verify/restore/scale/wait/cutover.
    t3 = time.time()
    r = fo.failover(target, backend, wait)
    step(3, "scale_gpu_pool", target=target, operator_confirmed_ts=t3, auto=auto,
         failover_ok=r.get("ok"), waited_s=r.get("waited_s"),
         failover_elapsed_s=r.get("elapsed_s"), aborted_at=r.get("aborted_at"))

    # 4. Chỉ ĐỌC kết quả state replica từ dict bước 3.
    after, rest = r.get("state_after") or {}, r.get("restore") or {}
    step(4, "verify_state_replica", target=target, vectors=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=rest.get("rpo_seconds"), docs_lost=rest.get("docs_lost"),
         embed_model_version=rest.get("embed_model_version"))

    # 5. Chỉ ĐỌC kết quả cutover.
    step(5, "dns_cutover", ok=r.get("ok"), cutover=r.get("cutover"))
    if not r.get("ok"):
        step(7, "post_incident", outcome="failover_aborted", reason=r.get("reason"),
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "failover": r}

    # 6. Golden signals: 10 request thật vào region phụ.
    g = golden_signals(target)
    step(6, "verify_golden_signals", target=target, **g,
         **{"pass": g["error_rate"] == 0 and g["p95_ms"] < 500})

    # 7. Post-incident
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", outcome="failed_over", elapsed_s=elapsed,
         rollback_to=primary,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": True, "elapsed_s": elapsed, "golden_signals": g, "failover": r}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--alert-wait", type=float, default=120,
                   help="chờ alert UNHEALTHY từ health checker tối đa N giây")
    p.add_argument("--wait", type=float, default=60, help="timeout chờ target /readyz")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto, a.alert_wait, a.wait),
                     indent=2))
