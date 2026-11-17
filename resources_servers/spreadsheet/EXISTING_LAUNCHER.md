# Dùng helper và giữ nguyên singlenode_main.sh

Chạy bước chuẩn bị trên **instance train**, bằng venv đang có. Runtime LibreOffice
tiếp tục nằm trên instance service riêng. Helper không cài package, copy cache,
convert lại data, cấp GPU hay tự chạy train. Helper sửa YAML với backup, đọc
`singlenode_main.sh` làm mẫu và tạo riêng `singlenode_spreadsheet.sh` cạnh nó.
Nội dung và permissions của `singlenode_main.sh` giữ nguyên.

## Những thay đổi cần thiết

| File/field | Giá trị hoặc hành vi |
| --- | --- |
| `data.train[0].data_path` | JSONL Spreadsheet đã convert, đủ tập train đang dùng |
| `env.nemo_gym.config_paths` | Model vLLM → resource/agent Spreadsheet → private override |
| `env.nemo_gym.uv_venv_dir` | Lấy từ `smoke.yaml` đã pass; tái dùng cache thực tế, không đoán path |
| `env` | Dùng `_override_` để bỏ các judge/server kế thừa không thuộc task; giữ cấu hình policy model |
| `policy.generation.vllm_cfg.http_server_serving_chat_kwargs.tool_parser` | Đọc chat template thật; Qwen3-4B đã test dùng `hermes` |
| `token_ids` | Đọc tokenizer thật; Qwen3-4B đã test EOS `151645`, không dùng `151643` của YAML cũ |
| `policy.megatron_cfg.sequence_parallel` | `false` khi TP=1 |
| Launcher mới | Tái dùng activation, copy/cache helper và NeMo-RL entrypoint từ launcher gốc; dùng driver đặt Ray log_dir vào IPC ngắn và truyền đúng exit code |

Không lấy batch size, số bước, giới hạn sinh, GPU memory hoặc tắt checkpoint từ
smoke sang full train. Với file đã cung cấp, `128 × 8`, `max_num_steps=676767`,
sequence length, logging và checkpoint settings được giữ nguyên. Helper kiểm
effective config trước khi ghi. Unrelated YAML comments cũng được giữ lại.

`smoke_driver.py` chỉ là wrapper đặt `init_ray(log_dir=...)` rồi gọi entrypoint
cũ với YAML đã chọn; bản thân driver không áp giới hạn hai bước hay bốn task.
Launcher được tạo giữ nguyên thư mục `SCRIPT_DIR` của bản gốc và tạo IPC root
riêng trong `/tmp` cho mỗi lần chạy, giữ log/checkpoint cũ
ở vị trí đã cấu hình và bypass proxy cho các địa chỉ service/model nội bộ.

## Lệnh chuẩn bị một lần

Checkout branch `tungks/feat/tool/spreadsheet-rl` của
`https://github.com/sontungkieu/Spreadsheet-RL` vào `spreadsheet_source` bên dưới.
Các đường dẫn là từ lần setup/smoke đã kiểm chứng; helper sẽ đọc lại files, cache
imports và service health trước khi ghi. Hostname/node hiện tại phải khớp.

```bash
spreadsheet_source=/workspace/storage-shared/nlp/tungks/spreadsheet_all/src/Spreadsheet-RL
spreadsheet_project=/workspace/storage-shared/nlp/tungks/spreadsheet_all
spreadsheet_nemo=/workspace/storage-shared/nlp/vai-1.4-30b/nemo-rl
spreadsheet_config_dir="$spreadsheet_nemo/rlvr_nemo_gym/v0_sizing"

bash "$spreadsheet_source/resources_servers/spreadsheet/packaging/prepare_existing_launcher.sh" \
  "$spreadsheet_nemo/custom_opt_ultra_selfbuilt_nano/nemo-rl-1109" \
  "$spreadsheet_nemo/custom_opt_ultra_selfbuilt_nano/nltk_data" \
  "$spreadsheet_config_dir/qwen_test.yaml" \
  --launcher "$spreadsheet_config_dir/singlenode_main.sh" \
  --smoke-config "$spreadsheet_project/runs/smoke_train_aa4cf4d/smoke.yaml" \
  --train-data "$spreadsheet_project/data/6f274cd4cbc27e656d7ddae7c3b591669866dc84/prepared-SP06/nemo_gym/train.jsonl" \
  --private-config "$spreadsheet_project/.secrets/spreadsheet/gym.private.yaml" \
  --expected-hostname vt-super-rl-test-ultra-selfbuilt-67-tra8-0-0
```

Thấy `EXISTING_SPREADSHEET_LAUNCHER_PREPARED` mới chạy launcher được helper tạo:

```bash
cd /workspace/storage-shared/nlp/vai-1.4-30b/nemo-rl/rlvr_nemo_gym/v0_sizing
bash singlenode_spreadsheet.sh
```

YAML có backup `.before-spreadsheet-full-<id>.bak`, mode 0600. Receipt cạnh YAML
ghi hash trước/sau, backup path, hash launcher gốc và launcher mới, tokenizer,
cache, số dòng và service health. Nếu ghi lỗi, helper khôi phục YAML và gỡ launcher
mới do chính nó tạo; launcher gốc luôn chỉ đọc. Nó từ chối ghi đè launcher mới có
sẵn; có thể chọn tên mới qua `--output-launcher`, cùng thư mục với bản gốc để giữ
đúng `SCRIPT_DIR`. Khi rollback, khôi phục YAML từ backup trong receipt; launcher
gốc không cần khôi phục.

## Phạm vi đã kiểm chứng

Pure migration tests và Bash syntax checks đã pass, gồm cả chính nội dung YAML/
launcher người dùng cung cấp. Test bảo toàn lịch full train, comments, cache
activation, exit code, backup/restore và việc không ghi vào launcher gốc.
Việc apply lên node hiện tại chưa được
thực hiện trong lượt này. Smoke trước chỉ kiểm chứng hai bước; cấu hình full
`128 × 8`, 32k context và mức concurrency gốc chưa qua stress test dài.

Tiếp tục dùng tập 5.282 train đã được service hiện tại qualify. Bốn task phục hồi
trên Modal không tự được thêm vào đây; chúng cần cập nhật service source/catalog
và xác nhận deployment riêng.
