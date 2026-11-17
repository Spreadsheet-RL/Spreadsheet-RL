# Setup và kiểm chứng runtime Linux

Code tích hợp nằm trên branch `tungks/feat/tool/spreadsheet-rl` của
[`sontungkieu/Spreadsheet-RL`](https://github.com/sontungkieu/Spreadsheet-RL/tree/tungks/feat/tool/spreadsheet-rl).
Lịch sử nhập từ Gym được ghi trong [IMPORTS.json](IMPORTS.json). Các hash thay đổi
khi áp dụng commit lên lịch sử upstream khác; manifest giữ cả hai hash.

Kết quả audit toàn bộ dataset, task phục hồi và giới hạn về selection bias nằm
trong [DATASET_AUDIT.md](DATASET_AUDIT.md). Các export phục hồi chưa thay dữ liệu
hoặc service đang chạy trên node công ty.

## Hai instance và phần cần cài

| Instance | Thành phần | Điều kiện |
| --- | --- | --- |
| Service spreadsheet | LibreOffice, Python sandbox, API và private catalog | Ubuntu 24.04 x86_64, root có chroot/setuid/setgid; không cần GPU hoặc Docker lồng |
| Train | NeMo RL/Gym, model, resource adapter và spreadsheet agent | Tái dùng runtime/venv train hiện có; gọi được IP:port của service |

Dataset/source có thể ở shared storage. Python root, SQLite/lock state và nơi
chạy LibreOffice phải ở filesystem local của service. Giữ private config ngoài
Git/public artifacts, mode 0600. Không cài runtime service vào container train.

## 1. Cài runtime service từ artifact đã khóa

[runtime.lock.json](packaging/runtime.lock.json) pin repository HF, immutable
revision, kích thước và SHA256. Artifact hiện tại có source `9dcc7e1` và các
`.deb`, wheel, Python root cần thiết. Không cần build lại dependency khi chỉ đổi
source/frontend/config.

Tải archive và `SHA256SUMS-repro1` từ **cùng revision trong lock**, qua proxy của node
nếu cần. Kiểm SHA256 ngoài archive, giải nén vào thư mục mới trên node-local
storage, rồi chạy `bash install_offline.sh` với quyền root. Installer kiểm tra
checksum nội bộ, cài `.deb` bằng `--no-download`, pip bằng `--no-index`, tạo venv
tại đường dẫn cuối và chạy probe chroot/UID/seccomp thật. Nó từ chối ghi đè venv
có sẵn. Reproducible installation không có nghĩa là ghi đè được một môi trường
đang chạy.

Các package Ubuntu có thể cập nhật thư viện hệ thống **trong container service**;
xem danh sách `debs/` trước khi cài. Khi giải nén ngay trên local storage, không
cần block copy Python root từ shared storage như lần xử lý ACL/permission trước.
Probe thất bại phải được xử lý ở filesystem/capabilities, không tắt isolation.

Runtime bundle là dependency artifact; dùng **source trên branch hiện tại** cho
các helper tích hợp mới. Source cũ trong bundle không có mọi helper/routing mới.
Chi tiết tải/cài và giới hạn có trong [README](README.md#build-and-install-offline).

## 2. Chuẩn bị data và chạy API

1. Tải dataset trực tiếp trên service/shared storage, pin revision và kiểm hash.
2. `packaging/prepare_dataset.py`: extract an toàn, tạo manifest, qualify oracle
   bằng LibreOffice, lọc train/test. Có checkpoint để tiếp tục run bị gián đoạn.
3. `packaging/start_service.py`: dùng bundle, qualified manifest/report, data root,
   Python-root receipt và service URL để tạo private config và chạy/reuse API.
   Dùng node-local service/state root. Nếu train chung shared storage, xuất
   `gym.private.yaml` bằng `--export-private-config`.
4. `packaging/check_live_service.py`: gọi đủ 12 tool bằng trusted canary, kiểm tra
   retry, hai rollout độc lập, Python isolation, recalc và hash seed/oracle.

Các flag bắt buộc và receipt được mô tả trong
[packaging/README.md](packaging/README.md). Private key không đi vào data/model.

## 3. Ghép vào trainer hiện có

`prepare_training_node.sh` dùng activation/cache helper sẵn có để cài hai
frontend, đăng ký config paths, convert qualified JSONL và kiểm tra imports.
Nếu không được ghi vào cache hệ thống, `prepare_project_runtime.sh` tạo bản sao
cache trong project và một YAML kế thừa, chỉ đổi `env.nemo_gym.uv_venv_dir`.

Đã có smoke test model Qwen3-4B qua service riêng: 2 bước train, 8 rollout,
12 model tool calls, 6 reward 0 và 2 reward 1, không có environment_error.
Ba tool_error do model gọi sai tham số được trả về để model tiếp tục. Đây là
kiểm chứng kết nối/tool/reward trên một workload nhỏ. Advantages đều 0 do reward
của hai rollout cùng prompt giống nhau; không phải bằng chứng policy học tốt hơn.

Config đã test là YAML smoke kế thừa, có parser/EOS đúng tokenizer, TP1 không
sequence_parallel, bỏ judge không dùng và IPC path ngắn. Base YAML/launcher full
train chưa được đồng bộ hết các sửa đó. Không dùng riêng việc setup/import pass
để kết luận chạy nguyên YAML full train cũ là đã được kiểm chứng.

## 4. Test reproducibility của runtime

Chạy `packaging/modal_reproduce.py` bằng Modal CLI/runtime **đã có**, với profile
được phép dùng, `--source-commit` đúng Git HEAD sạch và `--output-dir` mới.
Không tải/build dependency lên Mac và không dùng GPU. Ví dụ từ repo:

```bash
modal run --profile PROFILE resources_servers/spreadsheet/packaging/modal_reproduce.py::main \
  --source-commit "$(git rev-parse HEAD)" --output-dir /path/to/new/reproduction-result
```

Script tải bundle trên Modal, kiểm size/SHA256 rồi chạy hai container Ubuntu sạch
độc lập. Ubuntu base được pin bằng OCI digest. Hai verifier bị chặn mạng và:

- Cài runtime hoàn toàn offline; kiểm từng version `.deb` đúng artifact.
- So sánh Python distributions, critical ELF/UNO hashes và LibreOffice version.
- Dựng lại Python sandbox bằng builder trong bundle từ Python/deb và wheel đã
  khóa; so content/mode/symlink với sandbox đã phát hành.
- Chạy Python isolation, sửa công thức qua nhiều sheet, LibreOffice recalc, reward
  đúng 1/sai 0 và seed/oracle giữ nguyên.
- Chạy lại bộ Linux tests trong artifact bằng sandbox vừa dựng lại.
- So fingerprint runtime, sandbox và behavior giữa hai container.

Report JSON nhỏ được lưu local; install/test logs đầy đủ giữ trên Modal Volume.
Fingerprint bỏ qua absolute prefix, timestamps, bytecode và workspace tạm; nó
giữ file bytes, modes, symlink targets, package versions và kết quả scoring.

Phân biệt ba mức:

| Mức | Cách kiểm chứng |
| --- | --- |
| Source overlay có cùng bytes | `build_source_bundle.py` hai lần từ cùng clean commit; so SHA256 |
| Cài/dựng lại runtime từ artifact khóa | Hai clean offline installs và sandbox rebuild bằng `modal_reproduce.py` |
| Fresh network build archive giống từng byte | Builder cũ còn tag/apt resolver và timestamp; **chưa cam kết** mức này |

Không thay lock bằng hash mới chỉ để làm test pass. Một bundle mới cần được
qualify, pin revision/hash mới và chạy lại phép kiểm tra trên clean containers.

### Kiểm chứng bản sửa trước khi promote

Lần kiểm tra ngày 2026-11-17 phát hiện bundle cũ chạy được nhưng bốn file
glibc/loader trong Python root khác với khi dựng từ chính bộ `.deb` offline.
Builder đã tạo root trước khi installer cập nhật thư viện. `modal_build.py`
hiện dựng lại root sau offline install. Archive cũ và checksum cũ giữ nguyên.

Chế độ `--repair` chỉ chấp nhận đúng bốn khác biệt đã chẩn đoán, thay chúng từ
root vừa dựng và đã test, ghi provenance rồi tạo archive chuẩn hóa timestamp,
owner và thứ tự file. Nó không bỏ qua khác biệt khi so hash. Hai clean workers
phải cho cùng archive SHA256 và cùng fingerprints trước khi `--publish` được
phép upload vào repository runtime đã có; credential chỉ truyền qua Modal Secret.
Nếu upload gián đoạn, `::resume_publication --report-path REPORT --hf-python PYTHON`
tái dùng qualification report và archive trên Volume, không chạy lại test.

`runtime.candidate.lock.json` giữ snapshot đã dùng để test artifact sửa trước khi
promote. Test lại archive tải từ HF, không dùng repair mode:

```bash
SPREADSHEET_RUNTIME_LOCK=runtime.candidate.lock.json \
modal run --profile PROFILE resources_servers/spreadsheet/packaging/modal_reproduce.py::main \
  --source-commit "$(git rev-parse HEAD)" --output-dir /path/to/new/candidate-result
```

Chỉ promote candidate sau khi cả hai clean offline installs pass, root đã phát
hành bằng root dựng lại và đúng các fingerprint trong lock. Lock cũ được giữ
riêng để tái hiện lỗi ban đầu.

### Kết quả đã chạy ngày 2026-11-17

- Hai lần repair từ cùng parent artifact và cùng commit tạo archive **giống từng
  byte**, SHA256 `151ae1135a44aa8d8f993a9bfc22306bbb0e9c0283c7c6e31de8405f94fb15d4`.
- Archive được upload rồi tải lại từ HF revision
  `36f3a2cdd2506ca61979d90998d7df41d5845014`; hai container sạch khác cài offline,
  mỗi container **34 pass, 2 skip**, không failure/error.
- Sandbox 4.899 entries khớp bytes/mode/symlink với root dựng lại: **0 khác biệt**.
  Runtime/package, root và behavior fingerprints giống nhau giữa hai container
  và đúng lock. Chroot/UID/seccomp pass; công thức liên sheet cho 42, reward đúng
  1/sai 0; seed và oracle giữ nguyên.
- Default `runtime.lock.json` đã promote bản này. `runtime.legacy.lock.json`
  giữ artifact cũ để tái hiện bốn khác biệt. Không tự thay runtime đang chạy trên
  service instance; bộ cài mới dành cho lần triển khai tiếp theo vào thư mục sạch.

Receipt đã bỏ dữ liệu riêng nằm trong
[reproducibility-20261117.json](packaging/reproducibility-20261117.json), có source
commits, immutable artifact, task/call IDs và fingerprints. Report/log đầy đủ giữ
trong `spreadsheet-gym-runtime-builds/reproduction-results/` trên Modal Volume.
