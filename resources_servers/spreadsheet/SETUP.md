# Setup và kiểm chứng runtime Linux

Code tích hợp nằm trên branch `tungks/feat/tool/spreadsheet-rl` của
[`sontungkieu/Spreadsheet-RL`](https://github.com/sontungkieu/Spreadsheet-RL/tree/tungks/feat/tool/spreadsheet-rl).
Lịch sử nhập từ Gym được ghi trong [IMPORTS.json](IMPORTS.json). Các hash thay đổi
khi áp dụng commit lên lịch sử upstream khác; manifest giữ cả hai hash.

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

Tải archive và `SHA256SUMS` từ **cùng revision trong lock**, qua proxy của node
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
modal run --profile PROFILE resources_servers/spreadsheet/packaging/modal_reproduce.py \
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
