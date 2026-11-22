# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.25)
project(ocean_mlx_wired LANGUAGES CXX)

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Darwin" OR NOT CMAKE_SYSTEM_PROCESSOR STREQUAL "arm64")
    message(FATAL_ERROR "The wired MLX extension requires Apple Silicon macOS")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Python 3.11 COMPONENTS Interpreter Development.Module REQUIRED)
find_package(nanobind 2.13.0 EXACT CONFIG REQUIRED)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -c
        "from importlib.metadata import distribution; mlx = distribution('mlx'); assert mlx.version == '0.32.0', mlx.version; print(mlx.locate_file('mlx'))"
    OUTPUT_VARIABLE MLX_ROOT
    OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY
)
nanobind_add_module(_ocean_mlx_wired NB_STATIC NB_DOMAIN mlx bindings.cpp)
target_include_directories(_ocean_mlx_wired PRIVATE "${MLX_ROOT}/include")
target_link_libraries(_ocean_mlx_wired PRIVATE "${MLX_ROOT}/lib/libmlx.dylib")
set_target_properties(_ocean_mlx_wired PROPERTIES BUILD_RPATH "${MLX_ROOT}/lib")
