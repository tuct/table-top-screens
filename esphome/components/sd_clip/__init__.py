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
"""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import display, http_request, sd_spi
from esphome.const import CONF_HEIGHT, CONF_ID, CONF_WIDTH

CODEOWNERS = ["@tabletop_mini_screens"]
DEPENDENCIES = ["sd_spi", "http_request", "display"]

CONF_SD_ID = "sd_id"
CONF_DISPLAY_ID = "display_id"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_DIRECTORY = "directory"

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
        cv.GenerateID(CONF_SD_ID): cv.use_id(sd_spi.SdSpi),
        cv.GenerateID(CONF_DISPLAY_ID): cv.use_id(display.Display),
        cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
            http_request.HttpRequestComponent
        ),
        # Must match the panel exactly: a frame is blitted at 0,0 with no
        # scaling, so a mismatch would walk off the end of the framebuffer.
        cv.Required(CONF_WIDTH): cv.int_range(min=1, max=4096),
        cv.Required(CONF_HEIGHT): cv.int_range(min=1, max=4096),
        cv.Optional(CONF_DIRECTORY, default="/clip"): _validate_directory,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    cg.add(var.set_sd(await cg.get_variable(config[CONF_SD_ID])))
    cg.add(var.set_display(await cg.get_variable(config[CONF_DISPLAY_ID])))
    cg.add(var.set_http(await cg.get_variable(config[CONF_HTTP_REQUEST_ID])))
    cg.add(var.set_size(config[CONF_WIDTH], config[CONF_HEIGHT]))
    cg.add(var.set_directory(config[CONF_DIRECTORY]))
