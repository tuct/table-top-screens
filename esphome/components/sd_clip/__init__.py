"""Play a clip of raw RGB565 frames straight off an SD card.

Why raw, when raw is enormous:

* Network JPEG   -- ~33 KB a frame, but ~1.8 s to decode at 800x480. The
                    decode, not the transfer, is the bottleneck: ~0.33 fps.
* PSRAM cache    -- removes the network, but a decoded 800x480 frame is
                    768 KB, so PSRAM holds only tens of frames.
* Raw on SD      -- 768 KB a frame on a medium that has gigabytes, and zero
                    decode. The only cost left is the card read and the blit,
                    both of which are bounded by SPI clock rather than CPU.

At 20 MHz a 768 KB frame needs ~307 ms to read, so roughly 3 fps, and about
double that if the card is stable at 40 MHz. That is an order of magnitude
over the network path, and it is a hardware ceiling rather than a software
one -- which is why it is worth the storage.

Frames are `<directory>/%04d.565`, no header: every frame is exactly
width*height*2 bytes, so the index alone locates it.

MJPEG clips (sd_clip.h, "MJPEG clips") are the opposite trade: compressed
frames held in PSRAM and decoded straight to the panel. That works because the
slow part of ESPHome's decoder is its per-pixel output, not the JPEG decode
itself. `max_bytes` caps how much of a clip is held in memory.

Two decoders, chosen by `jpeg_decoder` (default `auto`):

* `software`  JPEGDEC, block callback straight to draw_pixels_at(). The only
              option on an ESP32-S3.
* `hardware`  the ESP32-P4's JPEG peripheral (esp_driver_jpeg), which decodes
              a whole frame to RGB565 in one call. Measured on a 480x800
              panel, software took 99-141 ms a frame -- about 7-10 fps, under
              the 15 fps the server resamples to.
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display, http_request, sd_spi
from esphome.components.esp32 import (
    VARIANT_ESP32P4,
    add_idf_component,
    get_esp32_variant,
)
from esphome.const import CONF_HEIGHT, CONF_ID, CONF_WIDTH

CODEOWNERS = ["@tabletop_mini_screens"]
# Not sd_spi: a board can hold content in PSRAM alone -- an ESP32-P4 has
# tens of megabytes of it, and its card is on SDMMC rather than SPI.
DEPENDENCIES = ["http_request", "display"]

CONF_SD_ID = "sd_id"
CONF_DISPLAY_ID = "display_id"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_DIRECTORY = "directory"
CONF_MAX_BYTES = "max_bytes"
CONF_CACHE_BYTES = "cache_bytes"
CONF_BYTE_ORDER = "byte_order"
CONF_JPEG_DECODER = "jpeg_decoder"
CONF_ELEMENT_ORDER = "element_order"

sd_clip_ns = cg.esphome_ns.namespace("sd_clip")
SdClip = sd_clip_ns.class_("SdClip", cg.Component)


def _validate_directory(value):
    value = cv.string_strict(value)
    if not value.startswith("/"):
        raise cv.Invalid("directory must be absolute within the card, e.g. /clip")
    if value.endswith("/"):
        raise cv.Invalid("directory must not end with a slash")
    return value


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(SdClip),
        cv.Optional(CONF_SD_ID): cv.use_id(sd_spi.SdSpi),
        cv.GenerateID(CONF_DISPLAY_ID): cv.use_id(display.Display),
        cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
            http_request.HttpRequestComponent
        ),
        # Must match the panel exactly: a frame is blitted at 0,0 with no
        # scaling, so a mismatch would walk off the end of the framebuffer.
        cv.Required(CONF_WIDTH): cv.int_range(min=1, max=4096),
        cv.Required(CONF_HEIGHT): cv.int_range(min=1, max=4096),
        cv.Optional(CONF_DIRECTORY, default="/clip"): _validate_directory,
        # PSRAM held by one MJPEG item. 4 MB is about a minute of 240x240 video
        # at 20 fps; frames past the cap are dropped whole.
        cv.Optional(CONF_MAX_BYTES, default=4_000_000): cv.int_range(
            min=64_000, max=32_000_000
        ),
        # PSRAM for all items together -- stills and clips switched between
        # without a download. Least recently shown goes first.
        cv.Optional(CONF_CACHE_BYTES, default=5_000_000): cv.int_range(
            min=64_000, max=32_000_000
        ),
        # Byte order of the RGB565 we hand to the panel. It must match what
        # the display driver expects, because the fast paths here blit raw:
        #   mipi_rgb / mipi_spi  big_endian (their draw_pixels_at checks it)
        #   mipi_dsi             little_endian -- it ignores the flag entirely
        #                        and copies straight into a little-endian
        #                        framebuffer, so a mismatch is not an error,
        #                        just wrong colours.
        cv.Optional(CONF_BYTE_ORDER, default="big_endian"): cv.one_of(
            "big_endian", "little_endian", lower=True
        ),
        # `auto` means the JPEG peripheral on an ESP32-P4 and JPEGDEC on
        # anything else. Only the P4 has the peripheral.
        cv.Optional(CONF_JPEG_DECODER, default="auto"): cv.one_of(
            "auto", "hardware", "software", lower=True
        ),
        # Hardware decoder only: which way round the colour channels come out.
        # BGR by default because that is what Waveshare's own P4 player uses
        # ("Use BGR order for LCD compatibility") and what this panel wants;
        # RGB gives an image with red and blue exchanged.
        cv.Optional(CONF_ELEMENT_ORDER, default="bgr"): cv.one_of(
            "rgb", "bgr", lower=True
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    if CONF_SD_ID in config:
        cg.add(var.set_sd(await cg.get_variable(config[CONF_SD_ID])))
    cg.add(var.set_display(await cg.get_variable(config[CONF_DISPLAY_ID])))
    cg.add(var.set_http(await cg.get_variable(config[CONF_HTTP_REQUEST_ID])))
    cg.add(var.set_size(config[CONF_WIDTH], config[CONF_HEIGHT]))
    cg.add(var.set_directory(config[CONF_DIRECTORY]))
    cg.add(var.set_max_bytes(config[CONF_MAX_BYTES]))
    cg.add(var.set_cache_bytes(max(config[CONF_CACHE_BYTES], config[CONF_MAX_BYTES])))
    cg.add(var.set_big_endian(config[CONF_BYTE_ORDER] == "big_endian"))
    cg.add(var.set_bgr_order(config[CONF_ELEMENT_ORDER] == "bgr"))

    decoder = config[CONF_JPEG_DECODER]
    is_p4 = get_esp32_variant() == VARIANT_ESP32P4
    if decoder == "auto":
        decoder = "hardware" if is_p4 else "software"
    elif decoder == "hardware" and not is_p4:
        raise cv.Invalid(
            "jpeg_decoder: hardware needs the JPEG peripheral, which only the "
            "ESP32-P4 has. Use 'software' (or 'auto').",
        )

    if decoder == "hardware":
        # esp_driver_jpeg ships with ESP-IDF; nothing to pull in, and JPEGDEC
        # is not linked at all on this path.
        cg.add_define("USE_SD_CLIP_HW_JPEG")
    else:
        # The same JPEGDEC, at the same version, that runtime_image pulls in
        # for online_image's JPEG format, so a device using both links one copy.
        cg.add_library("JPEGDEC", "1.8.4", "https://github.com/bitbank2/JPEGDEC#1.8.4")
        add_idf_component(name="espressif/esp-dsp", ref="1.7.1")
