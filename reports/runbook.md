# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

**Trigger:** alert `region-a UNHEALTHY` từ health checker (`reports/health-events.jsonl`,
`event=state_change, to=UNHEALTHY`). **Chạy tất cả lệnh từ thư mục gốc repo.**
**Mục tiêu:** RTO ≤ 300s, RPO ≤ 300s. Drill gần nhất đạt RTO 28.3s, RPO 6.01s / 3 doc.

**Cách nhanh (bán tự động, khuyến nghị):** một lệnh chạy cả 7 bước bên dưới. Lệnh sẽ hỏi
`y/N` trước khi cutover:

```bash
python3 dr/runbook.py --primary a --target b --backend fs
```

Nếu script lỗi, làm tay theo bảng:

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau 5s) và `tail -n 3 reports/health-events.jsonl` | `a.ready=false` 3 lần liên tiếp, VÀ health log có `"region": "a", "to": "UNHEALTHY"`, VÀ `b.alive=true`. Nếu b cũng chết thì **DỪNG**, escalate SEV0, không failover | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | Mở kênh `#inc-region-a-down`, ghi SEV1. Lấy `t_outage`: `grep '"action": "kill"' chaos/chaos-events.jsonl \| tail -1` (outage thật thì lấy ts của alert đầu tiên) | ts mở incident được ghi vào `reports/runbook-run.jsonl` (step 2, có `t_outage` + `notify_delay_s`) | On-call SRE (Incident Commander) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | `curl -s localhost:8002/v1/state` cho `"weights": true` và `count` > 0. Ghi lại `rpo_seconds` / `docs_lost`, `embed_model_version` phải khớp với region A (`vi-e5-base@v3`) | On-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi `until curl -sf localhost:8002/readyz >/dev/null; do sleep 1; done; echo READY` | `/readyz` của b trả 200 (`"ready": true`). Thường mất khoảng 6s warm-up. **Quá 60s chưa ready thì DỪNG, KHÔNG cutover**, escalate ML-Platform | On-call SRE + ML Platform |
| 5 | DNS/LB cutover | Chỉ làm sau khi bước 4 xong: `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa TTL 5s), và `curl -s localhost:8080/v1/infer` cho `"edge_region":"b"` | On-call SRE (cần IC confirm) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8002/v1/infer; done` | 10/10 trả `200`, p95 < 500ms, error rate = 0%. Drill gần nhất: p95 9.5ms, 0 lỗi | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null (PASS), `valid: true`, `warnings: []`. Viết `reports/postmortem.md` trong 48h | Incident Commander |

**Rollback (failover ngược về region A):**

- **Điều kiện, phải thoả CẢ BA:**
  1. Health checker đã ghi `region-a → HEALTHY` (3 lần OK liên tiếp) và giữ ổn định ít nhất **30 phút**.
  2. Dữ liệu region A đã được đồng bộ lại từ B: snapshot ngược `put --region b` rồi `get --region a`.
     Mọi doc ghi vào B trong thời gian sự cố phải có ở A.
  3. Thực hiện ngoài giờ cao điểm, có thông báo trước.
- **Ai quyết định:** chỉ Incident Commander, cùng với service owner. On-call không được tự
  rollback. Không bao giờ để automation tự động rollback, vì full-auto không có circuit
  breaker sẽ khiến traffic flap qua lại giữa 2 region (§4 Anti-Patterns).
- **Lệnh rollback:** chạy lại runbook theo chiều ngược:
  `python3 dr/runbook.py --primary b --target a --backend fs`.
- **Rollback khẩn cấp:** nếu region B fail golden signals ở bước 6 trong khi A đã sống lại
  thì IC được quyết định cutover ngay về A bằng `printf a > edge/active_region`.
- **Kill switch cho drill:** `python3 chaos/kill_region.py restore --region a --backend bare`.
