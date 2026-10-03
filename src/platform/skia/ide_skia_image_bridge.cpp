// The IDE's bounded AppKit/Skia preview-image ownership bridge.
//
// ImageIO decoding and resource-generation policy stay in the IDE's C/Elisa
// layers. This file only copies validated straight-alpha RGBA pixels into a
// Skia-owned raster image and releases that image when Elisa unbinds it.

#include <cstddef>
#include <cstdint>
#include <limits>

#include "include/core/SkImage.h"
#include "include/core/SkImageInfo.h"
#include "include/core/SkPixmap.h"

extern "C" std::size_t elisa_ide_skia_image_from_rgba32(const std::uint8_t* pixels,
                                                         std::size_t byte_length,
                                                         std::size_t width,
                                                         std::size_t height,
                                                         std::size_t row_stride) {
    constexpr std::size_t kMaximumDimension = 4096;
    if (pixels == nullptr || width == 0 || height == 0 ||
        width > kMaximumDimension || height > kMaximumDimension ||
        width > std::numeric_limits<std::size_t>::max() / 4) {
        return 0;
    }

    const std::size_t minimum_stride = width * 4;
    if (row_stride < minimum_stride ||
        height > std::numeric_limits<std::size_t>::max() / row_stride ||
        byte_length < row_stride * height) {
        return 0;
    }

    const SkImageInfo info = SkImageInfo::Make(
        static_cast<int>(width), static_cast<int>(height),
        kRGBA_8888_SkColorType, kUnpremul_SkAlphaType);
    const SkPixmap pixmap(info, pixels, row_stride);
    sk_sp<SkImage> image = SkImages::RasterFromPixmapCopy(pixmap);
    if (image == nullptr) return 0;
    return reinterpret_cast<std::size_t>(image.release());
}

extern "C" void elisa_ide_skia_image_release(std::size_t image_handle) {
    if (image_handle == 0) return;
    reinterpret_cast<SkImage*>(image_handle)->unref();
}
