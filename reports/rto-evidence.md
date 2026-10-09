# RTO/RPO Evidence — Lab 23 (Trần Xuân Đức · 2A202602768)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Drill chạy ngày 2026-10-09 (giờ UTC), bare mode, `--mock`, chaos `netblock` (SIGSTOP).
Mọi giây "+x" bên dưới tính từ `t_outage` của drill tương ứng; số do
`tools/measure_rto.py` tính ra, không phải ước lượng.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T03:51:53` | chaos kill region-a, mode netblock | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` (ReadTimeout, 2010.6 ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có (16/32 request fail, tất cả sau t_outage) | không có dòng `ok:true` nào sau t_outage | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `tools/measure_rto.py` |
| Restore region-a sau drill | SIGCONT | `chaos/kill_region.py restore --backend bare` | `chaos/chaos-events.jsonl:2` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 (`2026-10-09T03:52:50`) | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:3` |
| Snapshot restore xong | +17.3s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +23.65s (waited 6.32s) | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +23.7s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.3s** (served_by b) | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.3s | 300s (5 phút) | **PASS** (dư 271.7s) |
| RPO — Vector DB | 6.01s / 3 doc | 300s (5 phút) | **PASS** (dư 293.99s) |

Ghi chú:

- RPO lấy từ dòng `2_restore_snapshot` (`rpo_seconds: 6.01`, `docs_lost: 3`,
  `embed_model_version: embed-model=vi-e5-base@v3`), xem `reports/failover-events.jsonl:2`.
- Snapshot dùng để restore là chu kỳ replication thứ 2, xem `reports/replication.jsonl:2`.
- Tổng request trong drill 2: 157. Có 14 request fail, tất cả nằm trong khoảng +0.1s → +26.3s
  (dòng 25–38). Sau khi phục hồi không còn request nào fail.
- RPO dao động giữa các lần chạy, tuỳ chu kỳ `state/replicate.py` (mỗi 30s) rơi vào lúc nào
  so với lúc restore. Trường hợp xấu nhất xấp xỉ 30s cộng thời gian từ snapshot tới lúc restore.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0 | `interval_s=5 × threshold=3`, ghi trong `reports/health-events.jsonl:3` (0 → +15.0s) | Giảm interval xuống 2s hoặc threshold xuống 2. Đổi lại dễ báo động giả, dễ flap hơn. Có thể kết hợp tín hiệu từ lỗi thật của edge (passive health check) |
| Snapshot restore | 2.3 | detect → `2_restore_snapshot` xong (+15.0 → +17.33s): gồm runbook nhận alert, probe lại 1 lần (timeout 2s), rồi copy snapshot (khoảng 5ms). Xem `reports/failover-events.jsonl:2` | Probe xác nhận với timeout ngắn hơn. Pre-restore liên tục sang region phụ (warm standby) để lúc failover không phải copy |
| GPU pool warm-up | 6.3 | `waited_s=6.32` ở `4_wait_ready` (+17.33 → +23.65s), xem `reports/failover-events.jsonl:4` | Giữ region phụ ở `pool_state=full` sẵn (hot standby). Tốn tiền GPU idle nhưng bỏ được khoảng 6s |
| DNS/LB TTL cache | 4.7 | t_recovered − t_cutover (+23.65 → +28.3s), xem `reports/drill-2-withdr.jsonl:39` | Giảm `EDGE_TTL_SECONDS`. Hoặc cho LB chuyển upstream ngay khi health check báo lỗi, không đợi DNS hết TTL |
| **Tổng** | **28.3** | 15.0 + 2.3 + 6.3 + 4.7 = 28.3s, khớp `rto_measured_s` | Detect floor chiếm 53% RTO |
