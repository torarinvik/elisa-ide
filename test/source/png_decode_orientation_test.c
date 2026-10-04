#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

extern long designer_png_decode_rgba32(const char* path, unsigned char* output,
                                       size_t capacity, size_t* width,
                                       size_t* height, size_t* row_stride);

int main(int argc, char** argv) {
    if (argc != 2) return 2;

    unsigned char pixels[16] = {0};
    size_t width = 0;
    size_t height = 0;
    size_t row_stride = 0;
    const long result = designer_png_decode_rgba32(argv[1], pixels, sizeof(pixels),
                                                   &width, &height, &row_stride);
    if (result != 1 || width != 2 || height != 2 || row_stride != 8) {
        fprintf(stderr, "PNG decode returned invalid image metadata: %ld, %zu x %zu, stride %zu\n",
                result, width, height, row_stride);
        return 3;
    }

    for (size_t x = 0; x < width; x++) {
        const size_t top = x * 4;
        const size_t bottom = row_stride + x * 4;
        if (pixels[top] != 255 || pixels[top + 1] != 0 || pixels[top + 2] != 0 || pixels[top + 3] != 255 ||
            pixels[bottom] != 0 || pixels[bottom + 1] != 0 || pixels[bottom + 2] != 255 || pixels[bottom + 3] != 255) {
            fprintf(stderr, "PNG rows were flipped or pixel channels changed\n");
            return 4;
        }
    }

    puts("test png_decode_rgba32_top_down: ok");
    return 0;
}
