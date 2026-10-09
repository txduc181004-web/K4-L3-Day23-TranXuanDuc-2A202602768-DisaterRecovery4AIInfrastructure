# Postmortem — DR Drill Lab 23 (Trần Xuân Đức · 2A202602768)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

- **Sự cố:** Region A (primary) ngừng phản hồi (netblock, SIGSTOP), drill 2 ngày 2026-10-09.
- **Ảnh hưởng:** 14/157 request lỗi (ReadTimeout khoảng 2s mỗi request) trong 28.3s. Mất 3 document đã ingest nhưng chưa kịp replicate.
- **Kết quả:** failover tự động sang Region B. RTO và RPO đều PASS so với mục tiêu 300s.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T03:52:50.9 | 0 | outage bắt đầu (kill region-a, netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T03:52:50.9 | 0.1 | user đầu tiên bị ảnh hưởng (ReadTimeout 2010.6 ms) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T03:53:03.8 | 12.9 | replicate vẫn snapshot từ đĩa region-a, đây là snapshot dùng để restore | `reports/replication.jsonl:2` |
| 2026-10-09T03:53:05.8 | 15.0 | health check alert: region-a UNHEALTHY sau 3 lần fail liên tiếp | `reports/health-events.jsonl:3` |
| 2026-10-09T03:53:08.2 | 17.3 | operator confirm cutover (runbook `--auto`), failover bắt đầu `1_verify_target` | `reports/failover-events.jsonl:1` (log local: `reports/runbook-run.jsonl:1-3`) |
| 2026-10-09T03:53:08.2 | 17.3 | restore snapshot xong: RPO 6.01s / 3 doc | `reports/failover-events.jsonl:2` |
| 2026-10-09T03:53:14.5 | 23.65 | region-b ready sau 6.32s warm-up, DNS cutover a → b | `reports/failover-events.jsonl:4-5` |
| 2026-10-09T03:53:19.2 | 28.3 | resolved: request đầu tiên OK, served_by=b | `reports/drill-2-withdr.jsonl:39` |

Độ trễ thông báo (t_outage → operator biết tin) là 17.3s. Trong đó 15.0s là detect floor
của health check, 2.3s là thời gian runbook nhận alert và probe xác nhận lại.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.3s` · gap: `-271.7s` (còn dư 271.7s, PASS)
- RPO mục tiêu: 300s · đo được: `6.01s` (`3` doc bị mất) · gap: `-293.99s` (còn dư, PASS)
- **Bước tốn nhiều giây nhất:** `health-check detect floor`, 15.0s, chiếm 53% RTO.
  Nguyên nhân là thiết kế chống flapping. Phải có 3 lần fail liên tiếp, mỗi lần cách nhau 5s,
  và với netblock thì mỗi probe còn treo thêm 2s tới khi timeout. Đây là cái giá cố ý trả
  để tránh false positive. Bước tốn thứ hai là GPU warm-up (6.3s), do region B chạy kiểu
  pilot-light (`pool_state=warm`, không có dữ liệu) chứ không phải hot standby.

Phân rã RTO: detect 15.0s + restore 2.3s + warm-up 6.3s + DNS TTL 4.7s = 28.3s.
Chi tiết xem `reports/rto-evidence.md`.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user bị lỗi 28.3s?** Vì edge vẫn trỏ về region A cho tới khi DNS cutover
   (+23.7s), cộng thêm TTL cache 5s của edge.
2. **Vì sao cutover muộn tới +23.7s?** Vì phải chờ detect (15s), rồi region B mới bắt đầu
   restore và warm-up (6.3s). B là pilot-light: lúc bình thường không có data, không có weights.
3. **Vì sao B không được giữ sẵn sàng?** Vì kiến trúc chọn active-passive với chi phí thấp.
   Replication chỉ đẩy snapshot lên object store mỗi 30s, không restore liên tục vào B.
4. **Vì sao RPO trong drill chỉ có 6s, và con số đó có đáng tin không?** Không hoàn toàn.
   Chaos `netblock` chỉ dừng process serving. Đĩa của region A vẫn đọc được, nên
   `replicate.py` vẫn snapshot được sau khi outage đã xảy ra (`reports/replication.jsonl:2`,
   lúc +12.9s sau outage), và `ingest.py` vẫn ghi vào A. Trong outage thật, cả region mất,
   nên snapshot mới nhất là snapshot trước outage. RPO có thể lên tới khoảng 30s (bằng `--every`).
5. **Vậy bước nào sẽ thất bại trong outage thật?**
   - `2_restore_snapshot` tính RPO bằng cách đọc DB của primary (`snapshot.rpo`). Khi
     region chết hẳn thì không đọc được, `rpo_seconds` và `docs_lost` sẽ là null.
     Cần tính RPO từ `MANIFEST.json` cộng với log ingest ở upstream (hàng đợi hoặc nguồn sự thật).
   - Health checker và runbook đang chạy chung một máy với region A. Nếu cả máy hoặc cả
     region chết thì không còn ai phát alert. Health checker phải đặt ở region thứ ba
     hoặc dùng dịch vụ bên ngoài.
   - Ingest không có retry hay queue. Ghi vào A trong lúc outage sẽ mất luôn, thay vì
     được đẩy lại sang B sau khi failover.

**Root cause hệ thống:** chọn kiến trúc pilot-light với async replication mỗi 30s, và
health check/alerting nằm cùng failure domain với primary.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giữ region B ở hot standby: restore snapshot vào B sau mỗi chu kỳ replicate, `pool_state=full` sẵn | SRE / Platform | 2026-10-23 | RTO −8.6s (bỏ được 2.3s restore + 6.3s warm-up) |
| 2 | Giảm `EDGE_TTL_SECONDS` 5s → 1s. Edge tự chuyển upstream khi health checker báo UNHEALTHY | Network / Edge owner | 2026-10-23 | RTO khoảng −4s |
| 3 | Giảm `replicate --every` 30s → 10s, hoặc chuyển sang streaming replication/CDC | Data / Storage | 2026-11-06 | RPO worst-case −20s (30s → 10s) |
| 4 | Đưa ingest qua queue bền (Kafka/SQS). Sau failover thì replay phần offset chưa replicate vào B | Data / Ingest owner | 2026-11-06 | docs_lost → 0 (RPO hiệu dụng gần 0) |
| 5 | Chạy health checker ở failure domain riêng (region thứ ba, hoặc Route53 health check) | SRE | 2026-10-30 | Không giảm số giây, nhưng tránh trường hợp RTO = vô hạn khi mất alerting |
| 6 | Tính RPO từ `MANIFEST.json` + log upstream, không đọc DB của primary | DR owner | 2026-10-30 | Có RPO đo được ngay cả khi primary mất hẳn |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15s**. Thực tế detect ở +15.0s (`reports/health-events.jsonl:3`), tức
   15.0 / 28.3 ≈ **53%** RTO. Muốn RTO 5 phút (300s) thì về lý thuyết interval có thể lên
   tới khoảng 60–80s với threshold 3. Tuy vậy phải chừa thời gian cho restore, warm-up và TTL,
   và trong thực tế không nên để detect chiếm phần lớn ngân sách RTO.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây, và bạn trả giá gì (§4 flapping)?**
   Detect floor giảm từ 15s xuống 3s, nên RTO giảm khoảng **12s** (28.3s → khoảng 16s).
   Cái giá phải trả:
   - Một sự cố nhỏ chỉ 3s (GC pause, deploy rolling, network jitter) đã đủ để flip
     UNHEALTHY và kích hoạt failover. Nếu không có circuit breaker thì traffic sẽ flap
     qua lại giữa hai region (§4 Anti-Patterns).
   - Số probe tăng gấp 5 lần.
   - Timeout probe (2s) lớn hơn interval (1s), nên các probe chồng lên nhau, phải giảm
     timeout và lại tăng false positive.
   Cách hợp lý hơn là giữ threshold, giảm interval vừa phải (2s), và yêu cầu operator
   confirm trước cutover (đúng như runbook bán tự động đang làm).

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost = 3` nghĩa là 3 ticket khách gửi trong 6.01s cuối trước khi restore sẽ
   **biến mất vĩnh viễn**. Region B không có, region A không còn, khách sẽ hỏi lại mà hệ
   thống không có lịch sử. Trong outage thật con số này lớn hơn: tới khoảng 30s dữ liệu
   (một chu kỳ replicate), cộng thêm toàn bộ dữ liệu khách cố ghi trong 6 giờ outage nếu
   ingest không có queue để replay. Vì vậy action item 3 và 4 quan trọng hơn việc giảm
   RTO thêm vài giây. Với khách hàng, mất dữ liệu nghiêm trọng hơn chờ thêm 30s.
