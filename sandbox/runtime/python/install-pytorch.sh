#!/bin/bash
set -o errexit

torch_ver="$1"
cuda_ver_in="$2"
cuda_ver_trim="${cuda_ver_in//.}"
cuda_ver="cu${cuda_ver_trim:0:3}"
pair_ver="${torch_ver}+${cuda_ver}"

case $pair_ver in
  "1.10.1+cu111")
    pip install torch==1.10.1+cu111 torchvision==0.11.2+cu111 torchaudio==0.10.1+cu111 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "1.11.0+cu113")
    pip install torch==1.11.0+cu113 torchvision==0.12.0+cu113 torchaudio==0.11.0+cu113 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "1.12.1+cu113")
    pip install torch==1.12.1+cu113 torchvision==0.13.1+cu113 torchaudio==0.12.1+cu113 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "1.12.1+cu116")
    pip install torch==1.12.1+cu116 torchvision==0.13.1+cu116 torchaudio==0.12.1+cu116 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "1.13.1+cu116")
    pip install torch==1.13.1+cu116 torchvision==0.14.1+cu116 torchaudio==0.13.1+cu116 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "1.13.1+cu117")
    pip install torch==1.13.1+cu117 torchvision==0.14.1+cu117 torchaudio==0.13.1+cu117 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.0.0+cu117")
    pip install torch==2.0.0+cu117 torchvision==0.15.1+cu117 torchaudio==2.0.1+cu117 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.0.0+cu118")
    pip install torch==2.0.0+cu118 torchvision==0.15.1+cu118 torchaudio==2.0.1+cu118 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.0.1+cu117")
    pip install torch==2.0.1+cu117 torchvision==0.15.2+cu117 torchaudio==2.0.2+cu117 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.0.1+cu118")
    pip install torch==2.0.1+cu118 torchvision==0.15.2+cu118 torchaudio==2.0.2+cu118 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.1.0+cu118")
    pip install torch==2.1.0+cu118 torchvision==0.16.0+cu118 torchaudio==2.1.0+cu118 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.1.0+cu121")
    pip install torch==2.1.0+cu121 torchvision==0.16.0+cu121 torchaudio==2.1.0+cu121 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.2.1+cu118")
    pip install torch==2.2.1+cu118 torchvision==0.17.1+cu118 torchaudio==2.2.1+cu118 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  "2.2.1+cu121")
    pip install torch==2.2.1+cu121 torchvision==0.17.1+cu121 torchaudio==2.2.1+cu121 -f https://veturbo-cn-beijing.tos-cn-beijing.volces.com/pytorch-mirror/index.html
    ;;
  *)
    echo "unsupported pytorch verison ${pair_ver}"
    exit 1
    ;;
esac

pip cache purge
