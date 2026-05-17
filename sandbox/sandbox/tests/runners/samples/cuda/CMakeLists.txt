cmake_minimum_required(VERSION 3.10)
set(PROJECT main)


project(${PROJECT})
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_ARCHITECTURES 80)
set(CMAKE_NO_SYSTEM_FROM_IMPORTED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CUDA_ARCHITECTURES "80")
set(CMAKE_CUDA_COMPILER "/usr/local/cuda/bin/nvcc")
# PyTorch deps
execute_process(COMMAND python3 -c "import torch; print(torch.utils.cmake_prefix_path)"
                OUTPUT_VARIABLE TORCH_PREFIX_PATH
                OUTPUT_STRIP_TRAILING_WHITESPACE)
list(APPEND CMAKE_PREFIX_PATH ${TORCH_PREFIX_PATH})
find_package(Torch REQUIRED)
include_directories(${TORCH_INCLUDE_DIRS})

# CUDA deps
find_package(CUDA REQUIRED)
set(CUDA_HOST_COMPILATION_CPP ON)
CUDA_SELECT_NVCC_ARCH_FLAGS(ARCH_FLAGS Auto)
list(APPEND CUDA_NVCC_FLAGS ${ARCH_FLAGS})
include_directories(${CUDA_INCLUDE_DIRS})

# main executable
add_executable(${PROJECT} main.cu)
target_link_libraries(${PROJECT} ${TORCH_LIBRARIES} ${CUDA_LIBRARIES})
set_target_properties(${PROJECT} PROPERTIES CUDA_SEPARABLE_COMPILATION ON)
