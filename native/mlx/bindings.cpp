// SPDX-License-Identifier: MIT
#include <mlx/mlx.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/vector.h>

#include <atomic>
#include <cstdint>
#include <limits>
#include <memory>
#include <utility>
#include <vector>

namespace mx = mlx::core;
namespace nb = nanobind;

namespace {
struct BufferReference {
    std::atomic<bool> released{false};
};

using BoundArray = std::pair<mx::array, std::shared_ptr<BufferReference>>;

BoundArray bind(uintptr_t address, size_t bytes,
                const std::vector<int32_t>& dimensions, mx::Dtype type) {
    size_t expected = type.size();
    for (const auto dimension : dimensions) {
        if (dimension <= 0 || expected > std::numeric_limits<size_t>::max() / dimension) {
            throw nb::value_error("invalid wired tensor shape");
        }
        expected *= dimension;
    }
    if (!address || bytes != expected) {
        throw nb::value_error("wired tensor size mismatch");
    }

    const mx::Shape shape(dimensions.begin(), dimensions.end());
    auto reference = std::make_shared<BufferReference>();
    void* data = reinterpret_cast<void*>(address);
    const auto buffer = mx::allocator::make_buffer(data, bytes);
    if (!buffer.ptr()) {
        throw nb::value_error("MLX rejected the shared buffer");
    }
    auto array = mx::array(buffer, shape, type, [reference](auto owned) {
        mx::allocator::release(owned);
        reference->released.store(true, std::memory_order_release);
    });
    if (array.data<uint8_t>() != data) {
        throw nb::value_error("MLX changed the wired address");
    }
    return {std::move(array), std::move(reference)};
}
} // namespace

NB_MODULE(_ocean_mlx_wired, module) {
    nb::class_<BufferReference>(module, "BufferReference")
        .def_prop_ro("released", [](const BufferReference& reference) {
            return reference.released.load(std::memory_order_acquire);
        });
    module.def("bind", &bind);
}
